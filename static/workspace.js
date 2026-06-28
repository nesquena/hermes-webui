async function api(path,opts={}){
  // Strip leading slash so URL resolves relative to location.href (supports subpath mounts)
  const rel = path.startsWith('/') ? path.slice(1) : path;
  const url=new URL(rel,document.baseURI||location.href);
  const timeoutMs=Object.prototype.hasOwnProperty.call(opts,'timeoutMs')?opts.timeoutMs:30000;
  const timeoutToast=opts.timeoutToast!==false;
  const redirect401=opts.redirect401!==false;
  const maxAttempts=Object.prototype.hasOwnProperty.call(opts,'retries')?Math.max(0,Number(opts.retries)||0)+1:3;
  const retryTimeouts=opts.retryTimeouts===true;
  const retryStatuses=Array.isArray(opts.retryStatuses)?opts.retryStatuses.map(Number).filter(Number.isFinite):[];
  const retryDelayMs=Object.prototype.hasOwnProperty.call(opts,'retryDelayMs')?Math.max(0,Number(opts.retryDelayMs)||0):350;
  // Retry up to 2 times on network errors (e.g. stale keep-alive after long idle).
  // Callers may opt into retrying timeouts / transient server statuses for idempotent GETs.
  let lastErr;
  for(let attempt=0;attempt<maxAttempts;attempt++){
    let controller=null;
    let timeoutId=null;
    let didTimeout=false;
    let upstreamSignal=null;
    let upstreamAbort=null;
    try{
      const fetchOpts={...opts};
      delete fetchOpts.timeoutMs;
      delete fetchOpts.timeoutToast;
      delete fetchOpts.redirect401;
      delete fetchOpts.retries;
      delete fetchOpts.retryTimeouts;
      delete fetchOpts.retryStatuses;
      delete fetchOpts.retryDelayMs;

      const useTimeout=Number.isFinite(Number(timeoutMs))&&Number(timeoutMs)>0;
      if(useTimeout&&typeof AbortController!=='undefined'){
        controller=new AbortController();
        upstreamSignal=fetchOpts.signal||null;
        if(upstreamSignal){
          upstreamAbort=()=>controller.abort(upstreamSignal.reason);
          if(upstreamSignal.aborted) upstreamAbort();
          else upstreamSignal.addEventListener('abort',upstreamAbort,{once:true});
        }
        fetchOpts.signal=controller.signal;
      }
      const requestPromise=(async()=>{
        const res=await fetch(url.href,{credentials:'include',headers:{'Content-Type':'application/json'},...fetchOpts});
        if(!res.ok){
          // 401 means the auth session expired. Redirect to login so the user can
          // re-authenticate. This is especially important for iOS PWA (standalone mode)
          // and for subpath mounts like /hermes/, where /login escapes to the site root.
          if(res.status===401){
            if(redirect401) window.location.href='login?next='+encodeURIComponent(window.location.pathname+window.location.search);
            // Callers can opt out of navigation and handle the unauthenticated state themselves.
            return;
          }
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
        return ct.includes('application/json')?await res.json():await res.text();
      })();
      return useTimeout?await Promise.race([
        requestPromise,
        new Promise((_,reject)=>{
          timeoutId=setTimeout(()=>{
            didTimeout=true;
            if(controller) controller.abort();
            const err=new Error('Request timed out. Please try again.');
            err.name='TimeoutError';
            err.timeout=true;
            reject(err);
          },Number(timeoutMs));
        })
      ]):await requestPromise;
    }catch(e){
      lastErr=e;
      const isTimeout=didTimeout||(e&&(e.timeout===true||e.name==='TimeoutError'));
      if(isTimeout){
        if(retryTimeouts&&attempt<2&&attempt<maxAttempts-1){
          if(retryDelayMs) await new Promise(resolve=>setTimeout(resolve,retryDelayMs*Math.pow(2,attempt)));
          continue;
        }
        const err=(e&&e.name==='TimeoutError')?e:new Error('Request timed out. Please try again.');
        err.name='TimeoutError';
        err.timeout=true;
        if(timeoutToast&&typeof showToast==='function') showToast('Request timed out. Please try again.',5000,'error');
        throw err;
      }
      // Only retry on network errors (TypeError from fetch), not on HTTP errors
      // that were already thrown above. Re-throw 401 redirects immediately.
      if(e.message&&/401/.test(e.message)) throw e;
      if(attempt<2&&attempt<maxAttempts-1 && (e instanceof TypeError || retryStatuses.includes(Number(e.status)))){
        if(retryDelayMs) await new Promise(resolve=>setTimeout(resolve,retryDelayMs*Math.pow(2,attempt)));
        continue;
      }
      throw e;
    }finally{
      if(timeoutId) clearTimeout(timeoutId);
      if(upstreamSignal&&upstreamAbort) upstreamSignal.removeEventListener('abort',upstreamAbort);
    }
  }
  throw lastErr;
}

function recordClientSSEError(source, details={}){
  try{
    const payload={
      event:'sse_error',
      source:String(source||'unknown'),
      ready_state:details.ready_state,
      session_id:details.session_id||null,
      stream_id:details.stream_id||null,
      visibility_state:(typeof document!=='undefined'&&document.visibilityState)||'unknown',
      online:(typeof navigator!=='undefined'&&typeof navigator.onLine==='boolean')?navigator.onLine:null,
      url_path:(typeof location!=='undefined'&&location.pathname)||'/',
      reason:details.reason||'EventSource.onerror',
    };
    void api('/api/client-events/log',{method:'POST',body:JSON.stringify(payload),timeoutMs:3000,timeoutToast:false}).catch(()=>{});
  }catch(_){}
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

function _escapeGrantStore(){
  if(!S._escapeGrants) S._escapeGrants = Object.create(null);
  return S._escapeGrants;
}

function _normalizeWorkspaceRelPath(path){
  let raw = String(path || '').trim().replace(/\\/g, '/');
  if(!raw || raw === '.') return '.';
  if(raw.startsWith('/')) return '';
  const parts = [];
  for(const part of raw.split('/')){
    if(!part || part === '.') continue;
    if(part === '..'){
      if(parts.length) parts.pop();
      else return '';
      continue;
    }
    parts.push(part);
  }
  return parts.length ? parts.join('/') : '.';
}

function _isSameOrChildPath(base, path){
  const normalizedBase = _normalizeWorkspaceRelPath(base);
  const normalizedPath = _normalizeWorkspaceRelPath(path);
  if(!normalizedBase || !normalizedPath) return false;
  if(normalizedBase === '.') return true;
  return normalizedPath === normalizedBase || normalizedPath.startsWith(`${normalizedBase}/`);
}

function _workspaceEscapeGrantForPath(path){
  const grants = _escapeGrantStore();
  const normalizedPath = _normalizeWorkspaceRelPath(path);
  if(!normalizedPath || !S.session || !S.session.session_id) return null;
  const sessionId = S.session.session_id;
  let best = null;
  for(const root of Object.keys(grants)){
    const grant = grants[root];
    if(!grant || grant.sessionId !== sessionId) continue;
    if(grant.expiresAt && Date.now() >= grant.expiresAt){
      delete grants[root];
      continue;
    }
    if(!_isSameOrChildPath(root, normalizedPath)) continue;
    if(!best || root.length > best.root.length) best = {root, grant};
  }
  return best ? best.grant : null;
}

function _workspaceEscapeExactGrant(path){
  const normalizedPath = _normalizeWorkspaceRelPath(path);
  const grant = _workspaceEscapeGrantForPath(normalizedPath);
  if(!grant) return null;
  return grant.path === normalizedPath ? grant : null;
}

function _storeWorkspaceEscapeGrant(data){
  if(!S.session || !data || !data.token) return null;
  const grants = _escapeGrantStore();
  const root = _normalizeWorkspaceRelPath(data.path || '');
  if(!root) return null;
  const grant = {
    sessionId: S.session.session_id,
    path: root,
    token: String(data.token),
    expiresAt: Number(data.expires_at || 0) * 1000,
    isDir: !!data.is_dir,
  };
  grants[root] = grant;
  return grant;
}

function _clearWorkspaceEscapeGrant(path){
  const grants = S._escapeGrants;
  if(!grants) return;
  const root = _normalizeWorkspaceRelPath(path);
  if(root && grants[root]) delete grants[root];
}

function _workspacePathIsReadOnly(path){
  return !!_workspaceEscapeGrantForPath(path || S.currentDir || '.');
}

function _workspaceRouteForPath(path, kind, opts={}){
  if(!S.session) return '';
  const normalizedPath = _normalizeWorkspaceRelPath(path);
  const grant = _workspaceEscapeGrantForPath(normalizedPath);
  const sessionId = encodeURIComponent(S.session.session_id);
  const params = new URLSearchParams({session_id:S.session.session_id, path:normalizedPath || '.'});
  if(grant){
    params.set('token', grant.token);
    if(kind === 'raw' && opts.download) params.set('download', '1');
    if(kind === 'raw' && opts.inline) params.set('inline', '1');
    if(kind === 'list') return `/api/escape/list?${params.toString()}`;
    if(kind === 'read') return `/api/escape/file/read?${params.toString()}`;
    if(kind === 'raw') return `/api/escape/file/raw?${params.toString()}`;
  }
  if(kind === 'list') return `/api/list?session_id=${sessionId}&path=${encodeURIComponent(normalizedPath || '.')}`;
  if(kind === 'read') return `/api/file?session_id=${sessionId}&path=${encodeURIComponent(normalizedPath || '.')}`;
  if(kind === 'raw'){
    const extra = [];
    if(opts.download) extra.push('download=1');
    // Inline previews intentionally preserve a literal &inline=1 marker in this file.
    if(opts.inline) extra.push('inline=1');
    const suffix = extra.length ? `&${extra.join('&')}` : '';
    return `/api/file/raw?session_id=${sessionId}&path=${encodeURIComponent(normalizedPath || '.')}${suffix}`;
  }
  return '';
}

async function authorizeWorkspaceEscapeNavigation(item){
  if(!S.session || !item || !item.path) return null;
  const normalizedPath = _normalizeWorkspaceRelPath(item.path);
  const exactGrant = _workspaceEscapeExactGrant(normalizedPath);
  if(!exactGrant){
    const ok = await showConfirmDialog({
      title: item.name || normalizedPath,
      message: t('external_link_open_confirm'),
      confirmLabel: t('dialog_confirm_btn'),
      danger: false,
      hideCancel: true,
      focusCancel: false,
    });
    if(!ok) return null;
  }
  try{
    const data = await api('/api/escape/authorize', {
      method: 'POST',
      body: JSON.stringify({
        session_id: S.session.session_id,
        path: normalizedPath,
      }),
    });
    const grant = _storeWorkspaceEscapeGrant(data);
    if(!grant) throw new Error('Missing escape authorization token');
    showToast(t('external_link_read_only'), 2000);
    return grant;
  }catch(e){
    showToast(t('external_link_grant_expired') || (e && e.message ? e.message : String(e)), 5000, 'error');
    return null;
  }
}

let _workspacePanelActiveTab = 'files';
let _renderSessionArtifactsTimer = null;

function _setWorkspacePanelTabDataset(){
  const panel = document.querySelector('.rightpanel');
  if(panel) panel.dataset.activeTab = _workspacePanelActiveTab;
}

function scheduleRenderSessionArtifacts(){
  if(_renderSessionArtifactsTimer) clearTimeout(_renderSessionArtifactsTimer);
  _renderSessionArtifactsTimer = setTimeout(()=>{
    _renderSessionArtifactsTimer = null;
    renderSessionArtifacts();
  }, 100);
}

if(typeof document !== 'undefined'){
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _setWorkspacePanelTabDataset, {once:true});
  else _setWorkspacePanelTabDataset();
}

function switchWorkspacePanelTab(tab){
  // #2668 (opt-in, default off): the Git "Changes" tab is only valid when the
  // feature is enabled AND the current workspace is a git repo. Any other case
  // falls through to the existing Files/Artifacts/Todos behaviour unchanged.
  if(tab === 'changes'){
    const git = (typeof _ensureGitState === 'function') ? _ensureGitState() : null;
    if(window._workspaceGitEnabled && git && git.status && git.status.is_git){
      _workspacePanelActiveTab = 'changes';
      git.selectedTab = 'changes';
      git.selectedDiff = null;
      const pa = $('previewArea'); if(pa) pa.classList.remove('visible');
      const artifacts = $('workspaceArtifacts'); if(artifacts) artifacts.hidden = true;
      const todosPanel = $('workspaceTodosPanel'); if(todosPanel) todosPanel.hidden = true;
      if(typeof renderGitChanges === 'function') renderGitChanges();
      if(typeof renderWorkspacePanelTabState === 'function') renderWorkspacePanelTabState();
      return;
    }
    tab = 'files';
  }
  _workspacePanelActiveTab = tab === 'artifacts' ? 'artifacts' : tab === 'todos' ? 'todos' : 'files';
  if(typeof _ensureGitState === 'function') _ensureGitState().selectedTab = 'files';
  _setWorkspacePanelTabDataset();
  const filesTab = $('workspaceFilesTab');
  const artifactsTab = $('workspaceArtifactsTab');
  const todosTab = $('workspaceTodosTab');
  if(filesTab){
    filesTab.classList.toggle('active', _workspacePanelActiveTab === 'files');
    filesTab.setAttribute('aria-selected', _workspacePanelActiveTab === 'files' ? 'true' : 'false');
  }
  if(artifactsTab){
    artifactsTab.classList.toggle('active', _workspacePanelActiveTab === 'artifacts');
    artifactsTab.setAttribute('aria-selected', _workspacePanelActiveTab === 'artifacts' ? 'true' : 'false');
  }
  if(todosTab){
    todosTab.classList.toggle('active', _workspacePanelActiveTab === 'todos');
    todosTab.setAttribute('aria-selected', _workspacePanelActiveTab === 'todos' ? 'true' : 'false');
  }
  // #2668: deactivate the Git Changes tab and hide its view when leaving it.
  const changesTab = $('btnWorkspaceChangesTab');
  if(changesTab){
    changesTab.classList.remove('active');
    changesTab.setAttribute('aria-selected', 'false');
  }
  const changesView = $('gitChangesView'); if(changesView) changesView.style.display = 'none';
  const artifacts = $('workspaceArtifacts');
  if(artifacts) artifacts.hidden = _workspacePanelActiveTab !== 'artifacts';
  const todosPanel = $('workspaceTodosPanel');
  if(todosPanel) todosPanel.hidden = _workspacePanelActiveTab !== 'todos';
  if(_workspacePanelActiveTab === 'files' && typeof renderFileTree === 'function') renderFileTree();
  if(_workspacePanelActiveTab === 'artifacts') renderSessionArtifacts();
  if(_workspacePanelActiveTab === 'todos') _loadWorkspacePanelTodos();
}

function _loadWorkspacePanelTodos(){
  const panel = $('workspaceTodosPanel');
  if(!panel) return;
  let todos = [];
  try{
    if(S && Array.isArray(S.todos)){
      todos = S.todos;
    } else if(S && S.session && S.session.todo_state && Array.isArray(S.session.todo_state.todos)){
      todos = S.session.todo_state.todos;
    } else if(typeof _legacyTodosFromMessages === 'function'){
      todos = _legacyTodosFromMessages() || [];
    }
  }catch(e){ todos = []; }
  if(!todos.length){
    panel.innerHTML = '<div style="padding:24px 12px;text-align:center;color:var(--muted);font-size:12px">No active tasks</div>';
    return;
  }
  const statusIcon = (s) => {
    if(s === 'completed') return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--text)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>';
    if(s === 'in_progress') return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--blue)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>';
    if(s === 'cancelled') return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--muted)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    // pending
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--muted)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/></svg>';
  };
  const items = todos.map(t => {
    const s = t.status || 'pending';
    const isDone = s === 'completed' || s === 'cancelled';
    return `<div style="display:flex;align-items:flex-start;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">`+
      `<span style="flex-shrink:0;margin-top:2px">${statusIcon(s)}</span>`+
      `<span style="font-size:12px;color:${isDone?'var(--muted)':'var(--text)'};text-decoration:${s==='cancelled'?'line-through':'none'}">${_escHtml(t.content||t.text||'')}</span>`+
      `</div>`;
  }).join('');
  panel.innerHTML = `<div style="padding:4px 0">${items}</div>`;
}

function _escHtml(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

const ARTIFACT_IGNORE_RE = /(^|\/)(?:\.git|\.hg|\.svn|node_modules|\.venv|venv|__pycache__|dist|build|\.next|\.cache)(?:\/|$)/;
// Canonical Hermes mutators plus MCP filesystem aliases that can create/edit files.
const ARTIFACT_MUTATION_TOOLS = new Set(['write_file','patch','edit_file','create_file','mcp_filesystem_write_file','mcp_filesystem_edit_file']);

function _normalizeArtifactPath(path){
  if(!path) return '';
  path = String(path).trim().replace(/[\`"'<>),.;:]+$/g,'').replace(/^[\`"'(<]+/g,'');
  if(!path || path.length > 240 || path.includes('://')) return '';
  // Canonicalize workspace-relative prefixes so a file-tree open ("foo.md") and a
  // tool arg recorded as "./foo.md" or "~/foo.md" compare equal for mutation
  // tracking; otherwise an agent edit via a ./-prefixed path leaves the open
  // preview stale (#3262 / pre-release regression-gate finding).
  path = path.replace(/^~\//,'').replace(/^(?:\.\/)+/,'');
  if(!path) return '';
  if(ARTIFACT_IGNORE_RE.test(path)) return '';
  if(!/[./]/.test(path)) return '';
  return path;
}

function _artifactCandidatesFromText(text){
  if(!text || typeof text !== 'string') return [];
  const out = [];
  const seen = new Set();
  const add = (path) => {
    path = _normalizeArtifactPath(path);
    if(!path || seen.has(path)) return;
    seen.add(path); out.push({path, kind:'diff'});
  };
  // Fallback text mining is intentionally narrow: only diff/patch fences imply
  // the session changed a file. Prose mentions such as "edited package.json" are
  // too noisy for an Artifacts list that should track write/edit outputs.
  const fenced = /```(?:diff|patch)\s*\n[\s\S]*?```/gi;
  let m;
  while((m = fenced.exec(text))){
    const block = m[0];
    const fm = block.match(/(?:^|\n)(?:\+\+\+|---)\s+(?:[ab]\/)?([^\n\t]+)/);
    if(fm) add(fm[1].trim());
  }
  return out;
}

function _artifactCandidatesFromToolCall(tc){
  if(!tc) return [];
  const name = String(tc.name || '').replace(/^functions\./,'');
  const args = tc.arguments || tc.args || tc.input || {};
  const result = tc.result || tc.output || tc.snippet || '';
  const out = [];
  const add = (path, source=name || 'tool') => {
    path = _normalizeArtifactPath(path);
    if(path) out.push({path, kind:source});
  };
  if(ARTIFACT_MUTATION_TOOLS.has(name) && args && typeof args === 'object'){
    for(const key of ['path','file_path','source','destination']) add(args[key]);
    if(Array.isArray(args.paths)) args.paths.forEach(p=>add(p));
    if(Array.isArray(args.edits)) args.edits.forEach(e=>add(e&&e.path));
  }
  const resultText = typeof result === 'string' ? result : (result ? JSON.stringify(result) : '');
  // Tool results may include unified diffs from patch-style tools; scan those
  // narrowly after structured args so diff headers can still contribute paths.
  for(const a of _artifactCandidatesFromText(resultText)) out.push(a);
  if(!out.length && ARTIFACT_MUTATION_TOOLS.has(name)){
    const argsText = typeof args === 'string' ? args : JSON.stringify(args || {});
    for(const a of _artifactCandidatesFromText(argsText)) out.push(a);
  }
  return out;
}

const _turnMutatedPreviewPaths = new Set();

function resetTurnWorkspaceMutations(){
  _turnMutatedPreviewPaths.clear();
}

function noteWorkspaceMutationsFromToolCall(tc){
  for(const a of _artifactCandidatesFromToolCall(tc)){
    const path=_normalizeArtifactPath(a.path);
    if(path) _turnMutatedPreviewPaths.add(path);
  }
}

function noteWorkspaceMutationsFromToolCalls(toolCalls){
  if(!Array.isArray(toolCalls)) return;
  for(const tc of toolCalls) noteWorkspaceMutationsFromToolCall(tc);
}

function _isOpenPreviewPathMutated(){
  if(!_previewCurrentPath) return false;
  const current=_normalizeArtifactPath(_previewCurrentPath);
  return !!(current&&_turnMutatedPreviewPaths.has(current));
}

async function refreshOpenPreviewIfMutated(){
  if(typeof _previewDirty!=='undefined'&&_previewDirty) return;
  if(!_isOpenPreviewPathMutated()) return;
  if(!_previewCurrentPath||!S.session) return;
  await openFile(_previewCurrentPath, { bustCache: true });
}

function collectSessionArtifacts(){
  const items = [];
  const seen = new Set();
  const push = (path, source) => {
    path = _normalizeArtifactPath(path);
    if(!path || seen.has(path)) return;
    seen.add(path); items.push({path, source});
  };
  // Source 1: session-level tool call summaries (may be empty when messages
  // carry their own tool metadata — see _syncToolCallsForLoadedMessages).
  for(const tc of (S.toolCalls || [])){
    for(const a of _artifactCandidatesFromToolCall(tc)) push(a.path, a.kind || tc.name || 'tool');
  }
  // Source 2 & 3: message-level data — both text-mined diffs and structured
  // tool_calls / tool_use content blocks that survive the S.toolCalls clear.
  for(const msg of (S.messages || [])){
    if(!msg) continue;
    const text = msg.content || msg.text || msg.message || '';
    // Text-mined diff/patch fences (existing path).
    if(typeof text === 'string'){
      for(const a of _artifactCandidatesFromText(text)) push(a.path, a.kind);
    }
    // Structured tool_calls array (OpenAI format: {function:{name,arguments}}).
    if(Array.isArray(msg.tool_calls)){
      for(const tc of msg.tool_calls){
        if(!tc || typeof tc !== 'object') continue;
        const fn = (tc.function && typeof tc.function === 'object') ? tc.function : tc;
        const name = fn.name || tc.name || '';
        let args = fn.arguments || tc.arguments || tc.args || tc.input || {};
        if(typeof args === 'string'){ try{ args = JSON.parse(args); }catch(_){} }
        const fakeTc = {name, args, result: tc.result || tc.output || ''};
        for(const a of _artifactCandidatesFromToolCall(fakeTc)) push(a.path, a.kind || name || 'tool');
      }
    }
    // Structured content array with tool_use blocks (Anthropic format).
    if(Array.isArray(msg.content)){
      for(const block of msg.content){
        if(!block || block.type !== 'tool_use') continue;
        let inp = block.input || {};
        if(typeof inp === 'string'){ try{ inp = JSON.parse(inp); }catch(_){} }
        const fakeTc = {name: block.name || '', args: inp, result: block.result || ''};
        for(const a of _artifactCandidatesFromToolCall(fakeTc)) push(a.path, a.kind || block.name || 'tool');
      }
    }
  }
  return items.slice(0, 50);
}

function renderSessionArtifacts(){
  const root = $('workspaceArtifacts');
  const count = $('workspaceArtifactsCount');
  if(!root) return;
  const items = collectSessionArtifacts();
  if(count) count.textContent = String(items.length);
  if(!S.session){
    root.innerHTML = '<div class="workspace-artifact-empty">Open a conversation to see files changed in this session.</div>';
    return;
  }
  if(!items.length){
    root.innerHTML = '<div class="workspace-artifact-empty">No artifacts detected yet. Files created or edited during this session will appear here.</div>';
    return;
  }
  // Strip workspace prefix for display so long absolute paths don't clutter the list.
  const ws = S.session && S.session.workspace;
  const normWs = ws ? ws.replace(/\/+$/,'') + '/' : '';
  const displayPath = (p) => {
    if(normWs && p.startsWith(normWs)) return p.slice(normWs.length);
    return p;
  };
  root.innerHTML = items.map(item => `<button type="button" class="workspace-artifact-item" data-artifact-path="${esc(item.path)}" onclick="openArtifactPath(this.dataset.artifactPath)"><div class="workspace-artifact-path">${esc(displayPath(item.path))}</div><div class="workspace-artifact-meta">${esc(item.source || 'session')}</div></button>`).join('');
}

async function _workspacePathExists(path){
  if(!S.session||!path) return false;
  const parts=String(path).split('/').filter(Boolean);
  const name=parts.pop();
  if(!name) return false;
  const dir=parts.length?parts.join('/'):'.';
  const data=await api(`/api/list?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(dir)}`);
  return (data.entries||[]).some(entry=>entry&&((entry.path===path)||entry.name===name));
}

async function openArtifactPath(path){
  if(!path) return;
  switchWorkspacePanelTab('files');
  let rel = path.replace(/^~\//,'').replace(/^\.\/+/,'');
  // Strip workspace prefix so /api/list receives a workspace-relative path.
  const ws = S.session && S.session.workspace;
  if(ws){
    const normWs = ws.replace(/\/+$/,'') + '/';
    if(rel.startsWith(normWs)) rel = rel.slice(normWs.length);
    else if(rel === ws.replace(/\/+$/,'')) rel = '.';
  }
  if(!rel) rel = '.';
  try{
    if(!(await _workspacePathExists(rel))){
      setStatus(t('file_open_failed'));
      return;
    }
  }catch(_){
    setStatus(t('file_open_failed'));
    return;
  }
  openFile(rel);
}

// ── Workspace file-tree loading skeleton (#4662 Phase 1) ────────────────────
// During a profile switch the right-hand workspace panel would otherwise keep
// showing the previous profile's file tree until /api/list resolves. Show a
// clean tree-shaped skeleton in its place (panel stays open — hiding it is
// jarring). Varied bar widths + a small indent pattern so it reads as a real
// directory listing rather than a mechanical repeat.
const _WS_SKELETON_ROWS = [
  {w: 38, indent: 0, dir: true},
  {w: 72, indent: 0},
  {w: 44, indent: 1},
  {w: 63, indent: 1},
  {w: 80, indent: 0},
  {w: 51, indent: 1},
  {w: 67, indent: 0},
  {w: 39, indent: 1},
];

// Workspace-tree render generation. loadDir() captures this at call time and
// discards its render/cache writes if a newer generation started meanwhile.
// #4671 CORE: an empty-session profile switch REUSES the same session_id, so
// loadDir()'s session_id guard alone can't reject a pre-switch /api/list response
// that resolves after the new profile's loadDir('.') — it would paint the previous
// workspace's files over the switched-to profile. switchToProfile() bumps this
// UNCONDITIONALLY at switch start (even when the workspace panel is closed, since
// loadDir('.') still runs then), so the stale response is rejected.
let _wsTreeGen = 0;
function bumpWorkspaceTreeGen(){
  _wsTreeGen = (typeof _wsTreeGen === 'number' ? _wsTreeGen : 0) + 1;
  return _wsTreeGen;
}
if(typeof window!=='undefined') window.bumpWorkspaceTreeGen = bumpWorkspaceTreeGen;

function showWorkspaceTreeSkeleton(){
  const tree = $('fileTree');
  if(!tree) return;
  const wrap = document.createElement('div');
  wrap.className = 'skeleton-tree';
  wrap.setAttribute('aria-hidden', 'true');
  for(const spec of _WS_SKELETON_ROWS){
    const row = document.createElement('div');
    row.className = 'skeleton-tree-row';
    if(spec.indent) row.style.paddingLeft = (2 + spec.indent * 16) + 'px';
    const glyph = document.createElement('div');
    glyph.className = 'skeleton-glyph';
    const name = document.createElement('div');
    name.className = 'skeleton-bar skeleton-name';
    name.style.width = spec.w + '%';
    row.appendChild(glyph);
    row.appendChild(name);
    // Files (not dirs) show a size on the right; mirror that on leaf rows.
    if(!spec.dir){
      const size = document.createElement('div');
      size.className = 'skeleton-bar skeleton-size';
      row.appendChild(size);
    }
    wrap.appendChild(row);
  }
  tree.innerHTML = '';
  tree.appendChild(wrap);
  tree.style.display = '';
}

// Clear a stranded workspace-tree skeleton (#4662 Opus gate). showWorkspaceTreeSkeleton()
// is shown up front on a profile switch, but the real loadDir('.') that would
// replace it is skipped when the new profile has no bound workspace — leaving a
// shimmering skeleton forever. Call this on the no-workspace path so the tree
// empties instead. Only touches #fileTree when it still holds a skeleton, so
// it can't clobber a real render.
function clearWorkspaceTreeSkeleton(){
  const tree = $('fileTree');
  if(!tree) return;
  if(tree.querySelector('.skeleton-tree')) tree.innerHTML = '';
}

async function loadDir(path, opts={}){
  const preservePreview=!!(opts&&opts.preservePreview);
  const refreshExpanded=!!(opts&&opts.refreshExpanded);
  if(!S.session)return;
  const sessionId=S.session.session_id;
  const treeGen=_wsTreeGen;  // #4671: capture the workspace-tree generation. A profile
                             // switch bumps it (bumpWorkspaceTreeGen), so a stale response
                             // from the previous workspace — which would pass the session_id
                             // guard because an empty-session switch reuses the same id — is
                             // rejected here instead of painting the wrong profile's files.
  try{
    if(!path||path==='.'||refreshExpanded){
      S._dirCache={};
      _restoreExpandedDirs();  // restore per-workspace expanded state after root and refresh resets
    }
    S.currentDir=path||'.';
    const data=await api(
      _workspaceRouteForPath(path, 'list') ||
      `/api/list?session_id=${encodeURIComponent(sessionId)}&path=${encodeURIComponent(path||'.')}`
    );
    if(!S.session||S.session.session_id!==sessionId||treeGen!==_wsTreeGen)return;
    S.entries=data.entries||[];renderBreadcrumb();renderFileTree();
    // #2673 — refresh Artifacts tab when its source data (the file tree) updates.
    if(typeof renderSessionArtifacts==='function') renderSessionArtifacts();
    // Pre-fetch contents of restored expanded dirs so they render without a second click
    // (parallelized — avoids serial waterfall when multiple dirs are expanded)
    if(!path||path==='.'||refreshExpanded){
      const expanded=S._expandedDirs||new Set();
      const pending=[...expanded].filter(dirPath=>!S._dirCache[dirPath]);
      if(pending.length){
        const results=await Promise.all(pending.map(dirPath=>
          api(_workspaceRouteForPath(dirPath, 'list'))
            .then(dc=>({dirPath,entries:dc.entries||[]}))
            .catch(()=>({dirPath,entries:[]}))
        ));
        if(!S.session||S.session.session_id!==sessionId||treeGen!==_wsTreeGen)return;
        for(const {dirPath,entries} of results) S._dirCache[dirPath]=entries;
      }
      if(expanded.size>0)renderFileTree();
    }
    if(!preservePreview&&typeof clearPreview==='function'){
      if(typeof _previewDirty!=='undefined'&&_previewDirty){
        showConfirmDialog({title:t('unsaved_confirm'),message:'',confirmLabel:'Discard',danger:true,focusCancel:true}).then(ok=>{if(ok)clearPreview({keepPanelOpen:true});});
      }else{
        clearPreview({keepPanelOpen:true});
      }
    }else if(preservePreview){
      await refreshOpenPreviewIfMutated();
    }
    // Fetch git info for workspace root (non-blocking)
    // #2668: when the opt-in git feature is ON, the richer refreshGitStatus()
    // below owns #gitBadge — skip the legacy /api/git-info badge to avoid two
    // writers. When OFF this is byte-for-byte master behaviour.
    if((!path||path==='.') && !window._workspaceGitEnabled) _refreshGitBadge();
    // #2668 (opt-in, default off): refresh the rich git status for the Changes
    // tab + tree badges. refreshGitStatus() short-circuits to null when the
    // feature is off, so no /api/git/status call happens by default.
    if(window._workspaceGitEnabled&&typeof refreshGitStatus==='function') refreshGitStatus();
  }catch(e){
    const grant = _workspaceEscapeGrantForPath(path);
    if(grant && e && e.status===403){
      _clearWorkspaceEscapeGrant(grant.path);
      showToast(t('external_link_grant_expired') || t('file_open_failed'), 5000, 'error');
      return;
    }
    console.warn('loadDir',e);
  }
}

function refreshWorkspacePanel(){
  if(!S.session)return;
  const targetDir = S.currentDir || '.';
  loadDir(targetDir,{refreshExpanded:true});
}

async function _refreshGitBadge(){
  const badge=$('gitBadge');
  if(!badge||!S.session)return;
  const sessionId=S.session.session_id;
  try{
    const data=await api(`/api/git-info?session_id=${encodeURIComponent(sessionId)}`);
    if(!S.session||S.session.session_id!==sessionId)return;
    if(data.git&&data.git.is_git){
      const g=data.git;
      let text=g.branch||'git';
      if(g.dirty>0) text+=` \u00b7 ${g.dirty}\u2206`; // middot + delta
      if(g.behind>0) text+=` \u2193${g.behind}`;
      if(g.ahead>0) text+=` \u2191${g.ahead}`;
      badge.textContent=text;
      badge.className='git-badge'+(g.dirty>0?' dirty':'');
      badge.style.display='';
    } else {
      badge.style.display='none';
      badge.textContent='';
    }
  }catch(e){
    if(!S.session||S.session.session_id!==sessionId)return;
    badge.style.display='none';
  }
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
const MD_PREVIEW_RICH_RENDER_MAX_BYTES = 256 * 1024;
const MD_PREVIEW_RICH_RENDER_MAX_LINES = 5000;
// Binary formats that should download rather than preview
const DOWNLOAD_EXTS = new Set([
  '.docx','.doc','.xlsx','.xls','.pptx','.ppt','.odt','.ods','.odp',
  '.zip','.tar','.gz','.bz2','.7z','.rar',
  '.exe','.dmg','.pkg','.deb','.rpm',
  '.woff','.woff2','.ttf','.otf','.eot',
  '.bin','.dat','.db','.sqlite','.pyc','.class','.so','.dylib','.dll',
]);

function fileExt(p){ const i=p.lastIndexOf('.'); return i>=0?p.slice(i).toLowerCase():''; }

function markdownPreviewByteLength(content){
  const text=String(content||'');
  if(typeof Blob==='function') return new Blob([text]).size;
  if(typeof TextEncoder==='function') return new TextEncoder().encode(text).length;
  return unescape(encodeURIComponent(text)).length;
}

function markdownPreviewLineCount(content){
  const text=String(content||'');
  if(!text) return 1;
  return text.split('\n').length;
}

function shouldRenderMarkdownPreviewAsPlainText(content){
  return markdownPreviewByteLength(content)>MD_PREVIEW_RICH_RENDER_MAX_BYTES
    || markdownPreviewLineCount(content)>MD_PREVIEW_RICH_RENDER_MAX_LINES;
}

function largeMarkdownPlainTextStatus(content){
  const bytes=markdownPreviewByteLength(content);
  const lines=markdownPreviewLineCount(content);
  const sizeLabel=bytes>=1024?`${Math.round(bytes/1024)} KB`:`${bytes} B`;
  return `Large markdown file (${sizeLabel}, ${lines} lines) shown as plain text. Click "Render as markdown anyway" to force rich rendering, or Edit to view raw.`;
}

function setLargeMarkdownForceRenderVisible(visible){
  const btn=$('btnRenderMarkdownAnyway');
  if(btn) btn.style.display=visible?'inline-flex':'none';
}

function renderMarkdownPreviewContent(data){
  const target=data&&data.el?data.el:$('previewMd');
  if(!data||!data.el) showPreview('md');
  target.innerHTML=renderMd(data.content);
  requestAnimationFrame(()=>{if(typeof renderKatexBlocks==='function')renderKatexBlocks();});
}

function renderCodePreviewContent(path, content){
  showPreview('code');
  const codeEl=document.createElement('code');
  codeEl.textContent=content;
  const lang=_prismLanguageForPath(path);
  if(lang) codeEl.className='language-'+lang;
  const pre=$('previewCode');
  pre.textContent='';
  // Prism.highlightElement() propagates the language-* class onto the
  // parent <pre>, so a previously-previewed code file leaves e.g.
  // "language-css" on #previewCode. A subsequent plain-text file builds a
  // class-less <code>, and Prism walks up to that stale ancestor class and
  // mis-highlights prose. Strip any inherited language-* token from the
  // <pre> before each render so highlighting never leaks across files.
  pre.className=pre.className.replace(/\blanguage-\S+/g,'').replace(/\s+/g,' ').trim();
  pre.appendChild(codeEl);
  // Only invoke Prism when we actually assigned a language; otherwise the
  // class-less <code> would inherit any ancestor language-* class.
  if(lang&&typeof Prism!=='undefined'&&typeof Prism.highlightElement==='function'){
    Prism.highlightElement(codeEl);
  }
}

function renderCsvPreviewContent(path, content){
  if(typeof buildCsvTablePreview!=='function') return false;
  const preview=buildCsvTablePreview(path, content);
  if(!preview) return false;
  showPreview('csv');
  // Preserve the raw CSV text so the Edit flow can repopulate the textarea and
  // a save can re-render the table from the edited source (#4025 review, Codex).
  if(typeof content==='string'){
    _previewRawContent = content;
    _previewRawContentPath = path;
  }
  if(preview.html){
    $('previewMd').innerHTML=preview.html;
    return true;
  }
  if(preview.errorKey&&typeof _csvPreviewErrorHtml==='function'){
    $('previewMd').innerHTML=_csvPreviewErrorHtml(path, preview.errorKey);
    return true;
  }
  return false;
}

function forceRenderMarkdownPreview(){
  // #3378 review (Codex): don't force-render from a dirty/open editor — the
  // cached raw content would not reflect the unsaved edit. Require a saved,
  // non-dirty state and cached content that belongs to the current file.
  if(_previewDirty || $('previewEditArea').style.display!=='none') return;
  if(!_previewRawContent || _previewRawContentPath!==_previewCurrentPath) return;
  openFile(_previewCurrentPath,{forceRichMarkdown:true});
  setStatus('Markdown rendered for this file.');
}

let _previewCurrentPath = '';  // relative path of currently previewed file
let _previewCurrentMode = '';  // 'code' | 'csv' | 'md' | 'image' | 'html' | 'pdf' | 'audio' | 'video'
let _previewDirty = false;     // true when edits are unsaved

function showPreview(mode){
  // mode: 'code' | 'csv' | 'image' | 'md' | 'html' | 'pdf' | 'audio' | 'video' | 'gitdiff'
  // #2668 (opt-in): 'gitdiff' shows the git split/unified diff surface. All
  // other modes are unchanged from master.
  $('previewCode').style.display     = mode==='code'  ? '' : 'none';
  $('previewImgWrap').style.display  = mode==='image' ? '' : 'none';
  const mediaWrap=$('previewMediaWrap'); if(mediaWrap) mediaWrap.style.display = (mode==='audio'||mode==='video') ? '' : 'none';
  const pdfWrap=$('previewPdfWrap'); if(pdfWrap) pdfWrap.style.display = mode==='pdf' ? '' : 'none';
  $('previewMd').style.display       = (mode==='md'||mode==='csv') ? '' : 'none';
  $('previewHtmlWrap').style.display = mode==='html'  ? '' : 'none';
  $('previewEditArea').style.display = 'none';  // start in read-only
  const diffView=$('gitDiffView'); if(diffView) diffView.style.display = mode==='gitdiff' ? 'flex' : 'none';
  const badge=$('previewBadge');
  badge.className='preview-badge '+mode;
  badge.textContent = mode==='image'?'image':mode==='audio'?'audio':mode==='video'?'video':mode==='pdf'?'pdf':mode==='csv'?'csv':mode==='md'?'md':mode==='html'?'html':mode==='gitdiff'?'diff':fileExt($('previewPathText').textContent)||'text';
  _previewCurrentMode = mode;
  _previewDirty = false;
  updateEditBtn();
  // Show "Open in browser" button for iframe-backed document previews
  const openBtn=$('btnOpenInBrowser');
  if(openBtn) openBtn.style.display = (mode==='html'||mode==='pdf')?'inline-flex':'none';
  // #2668: hide the Download/Edit chrome on the read-only git diff surface.
  const downloadBtn=$('btnDownloadFile');
  if(downloadBtn) downloadBtn.style.display = mode==='gitdiff'?'none':'inline-flex';
  setLargeMarkdownForceRenderVisible(false);
}

function updateEditBtn(){
  const btn=$('btnEditFile');
  if(!btn)return;
  const editable = !_workspacePathIsReadOnly(_previewCurrentPath)
    && (_previewCurrentMode==='code'||_previewCurrentMode==='md'||_previewCurrentMode==='csv');
  btn.style.display = editable?'':'none';
  const editing = $('previewEditArea').style.display!=='none';
  btn.innerHTML = editing ? `&#128190; ${t('save')}` : `&#9998; ${t('edit')}`;
  btn.title = editing ? t('save_title') : t('edit_title');
  btn.style.color = editing ? 'var(--blue)' : '';
  if(_previewDirty) btn.innerHTML = '&#128190; Save*';
}

async function toggleEditMode(){
  const editing = $('previewEditArea').style.display!=='none';
  if(_workspacePathIsReadOnly(_previewCurrentPath)){
    showToast(t('external_link_read_only'), 2000);
    return;
  }
  if(editing){
    // Save
    if(!S.session||!_previewCurrentPath)return;
    const content=$('previewEditArea').value;
    try{
      await api('/api/file/save',{method:'POST',body:JSON.stringify({
        session_id:S.session.session_id, path:_previewCurrentPath, content
      })});
      _previewDirty=false;
      // Update read-only views AND the cached raw content so a later
      // "Render as markdown anyway" force-render reflects the just-saved text
      // (not the stale pre-edit fetch). #3378 review (Codex).
      _previewRawContent = content;
      _previewRawContentPath = _previewCurrentPath;
      if(_previewCurrentMode==='code') $('previewCode').textContent=content;
      else if(_previewCurrentMode==='csv') renderCsvPreviewContent(_previewCurrentPath, content);
      else renderMarkdownPreviewContent({content});
      $('previewEditArea').style.display='none';
      if(_previewCurrentMode==='code') $('previewCode').style.display='';
      else $('previewMd').style.display='';
      showToast(t('saved'));
    }catch(e){setStatus(t('save_failed')+e.message);}
  }else{
    // Enter edit mode: populate textarea with current content
    const currentText = _previewCurrentMode==='code'
      ? $('previewCode').textContent
      : _previewRawContent||'';
    $('previewEditArea').value=currentText;
    $('previewEditArea').style.display='';
    if(_previewCurrentMode==='code') $('previewCode').style.display='none';
    else $('previewMd').style.display='none';
    // Escape cancels the edit without saving
    $('previewEditArea').onkeydown=e=>{
      if(e.key==='Escape'){e.preventDefault();cancelEditMode();}
    };
  }
  updateEditBtn();
}

let _previewRawContent = '';  // raw text for md files (to populate editor)
let _previewRawContentPath = '';  // path that _previewRawContent belongs to (#3378 force-render cache guard)

function cancelEditMode(){
  // Discard changes and return to read-only view
  $('previewEditArea').style.display='none';
  $('previewEditArea').onkeydown=null;
  if(_previewCurrentMode==='code') $('previewCode').style.display='';
  else $('previewMd').style.display='';
  _previewDirty=false;
  updateEditBtn();
}

// Map file extensions to Prism.js language identifiers.
// Prism autoloader fetches missing language components from CDN on demand.
const _PRISM_LANG_MAP={
  js:'javascript',mjs:'javascript',jsx:'jsx',ts:'typescript',tsx:'tsx',
  py:'python',pyw:'python',pyi:'python',
  rb:'ruby',go:'go',rs:'rust',java:'java',kt:'kotlin',kts:'kotlin',
  c:'c',h:'c',cpp:'cpp',cxx:'cpp',hpp:'cpp',cc:'cpp',
  cs:'csharp',swift:'swift',scala:'scala',
  php:'php',pl:'perl',pm:'perl',r:'r',lua:'lua',
  sh:'bash',bash:'bash',zsh:'bash',fish:'bash',
  ps1:'powershell',psm1:'powershell',
  sql:'sql',graphql:'graphql',
  json:'json',yaml:'yaml',yml:'yaml',toml:'toml',xml:'xml',
  html:'markup',htm:'markup',svg:'markup',vue:'markup',
  css:'css',scss:'scss',sass:'sass',less:'less',
  md:'markdown',markdown:'markdown',
  dockerfile:'docker',makefile:'makefile',cmake:'cmake',
  ini:'ini',cfg:'ini',conf:'ini',properties:'properties',
  diff:'diff',patch:'diff',
  txt:'',log:'',csv:'',tsv:'',
};
const _PRISM_BASENAME_LANG_MAP={
  'dockerfile':'docker','makefile':'makefile','gnumakefile':'makefile',
  'cmakelists.txt':'cmake',
  '.gitignore':'ignore','.dockerignore':'ignore',
};
function _prismLanguageForPath(path){
  const base=String(path||'').split(/[\\/]/).pop().toLowerCase();
  if(base.startsWith('dockerfile.')) return 'docker';
  if(_PRISM_BASENAME_LANG_MAP[base]!==undefined) return _PRISM_BASENAME_LANG_MAP[base];
  const ext=fileExt(path).replace(/^\./,'');
  return _PRISM_LANG_MAP[ext]!==undefined?_PRISM_LANG_MAP[ext]:'plaintext';
}

async function openFile(path, opts={}){
  if(!S.session)return;
  const ext=fileExt(path);
  const bustCache=!!(opts&&opts.bustCache);
  const forceRichMarkdown=!!(opts&&opts.forceRichMarkdown);
  const cacheBust=bustCache?`&_=${Date.now()}`:'';

  // Binary/download-only formats: trigger browser download, don't preview
  if(DOWNLOAD_EXTS.has(ext)){
    downloadFile(path);
    return;
  }

  $('previewPathText').textContent=path;
  $('previewArea').classList.add('visible');
  $('fileTree').style.display='none';

  _previewCurrentPath = path;
  renderFileBreadcrumb(path);
  if(IMAGE_EXTS.has(ext)){
    // Image: load via raw endpoint, show as <img>
    showPreview('image');
    const url=_workspaceRouteForPath(path, 'raw') + cacheBust;
    $('previewImg').alt=path;
    $('previewImg').src=url;
    $('previewImg').onerror=()=>setStatus(t('image_load_failed'));
  } else if(AUDIO_EXTS.has(ext)||VIDEO_EXTS.has(ext)){
    const mode=VIDEO_EXTS.has(ext)?'video':'audio';
    showPreview(mode);
    const url=_workspaceRouteForPath(path, 'raw', {inline:true}) + cacheBust;
    const wrap=$('previewMediaWrap');
    if(wrap){
      wrap.innerHTML=(typeof _mediaPlayerHtml==='function')
        ? _mediaPlayerHtml(mode,url,path.split('/').pop()||path)
        : `<${mode} src="${url.replace(/"/g,'%22')}" controls preload="metadata"></${mode}>`;
      if(typeof _applyMediaPlaybackPreferences==='function') _applyMediaPlaybackPreferences(wrap);
    }
  } else if(PDF_EXTS.has(ext)){
    showPreview('pdf');
    const url=_workspaceRouteForPath(path, 'raw', {inline:true}) + cacheBust;
    const frame=$('previewPdfFrame');
    if(frame){
      frame.src=''; // clear first to avoid stale content
      frame.src=url;
      frame.title=`PDF preview: ${path.split('/').pop()||path}`;
    }
  } else if(MD_EXTS.has(ext)){
    // Markdown: fetch text, render with renderMd, display as formatted HTML
    try{
      // #3378 review (Codex): only reuse cached raw content when it actually
      // belongs to the requested path. `path===_previewCurrentPath` is tautological
      // here (_previewCurrentPath was just assigned above), so guard on the
      // dedicated _previewRawContentPath instead — otherwise a force-render after a
      // file switch could re-render the previous file's cached content.
      const data=forceRichMarkdown&&path===_previewRawContentPath&&_previewRawContent
        ? {content:_previewRawContent}
        : await api(_workspaceRouteForPath(path, 'read'));
      _previewRawContent = data.content;
      _previewRawContentPath = path;
      if(!forceRichMarkdown && shouldRenderMarkdownPreviewAsPlainText(data.content)){
        showPreview('code');
        $('previewCode').textContent=data.content;
        setLargeMarkdownForceRenderVisible(true);
        setStatus(largeMarkdownPlainTextStatus(data.content));
        return;
      }
      renderMarkdownPreviewContent(data);
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
    const url=_workspaceRouteForPath(path, 'raw', {inline:true}) + cacheBust;
    const iframe=$('previewHtmlIframe');
    if(iframe){
      iframe.src=''; // clear first to avoid stale content
      iframe.src=url;
    }
  } else if(ext==='.csv'){
    try{
      const data=await api(_workspaceRouteForPath(path, 'read'));
      if(data.binary){
        downloadFile(path);
        return;
      }
      if(renderCsvPreviewContent(path, data.content)) return;
      renderCodePreviewContent(path, data.content);
    }catch(e){
      downloadFile(path);
    }
  } else {
    // Plain code / text -- but fall back to download if server signals binary
    try{
      const data=await api(_workspaceRouteForPath(path, 'read'));
      if(data.binary){
        // Server flagged this as binary content
        downloadFile(path);
        return;
      }
      renderCodePreviewContent(path, data.content);
  }catch(e){
      const grant = _workspaceEscapeGrantForPath(path);
      if(grant && e && e.status===403){
        _clearWorkspaceEscapeGrant(grant.path);
        showToast(t('external_link_grant_expired') || t('file_open_failed'), 5000, 'error');
        return;
      }
      // If it's a 400/too-large error, offer download instead
      downloadFile(path);
    }
  }
}

function downloadFile(path){
  if(!S.session)return;
  // Trigger browser download via the raw file endpoint with content-disposition attachment
  const url=_workspaceRouteForPath(path, 'raw', {download:true});
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
  const url=_workspaceRouteForPath(_previewCurrentPath, 'raw', {inline:true});
  window.open(url,'_blank','noopener');
}
// openInBrowser keeps the helper-based raw path, which expands to an explicit &inline=1 URL.

// ── Workspace upload ──────────────────────────────────────────────────
function triggerWorkspaceUpload() {
  if(_workspacePathIsReadOnly(S.currentDir || '.')){
    showToast(t('external_link_read_only'), 2000);
    return;
  }
  const input = $('workspaceFileInput');
  if (!input) return;
  input.value = '';
  input.onchange = async () => {
    const files = input.files;
    if (!files || !files.length) return;
    for (const file of files) {
      await uploadToWorkspace(file, S.currentDir || '.');
    }
    if (S.session) loadDir(S.currentDir);
  };
  input.click();
}

async function uploadToWorkspace(file, dir) {
  if (!S.session) return;
  if(_workspacePathIsReadOnly(dir || '.')){
    showToast(t('external_link_read_only'), 2000);
    return;
  }
  const formData = new FormData();
  formData.append('session_id', S.session.session_id);
  formData.append('path', dir || '.');
  formData.append('file', file, file.name);
  try {
    showToast(t('uploading') || 'Uploading\u2026', 2000);
    const data = await api('/api/workspace/upload', {
      method: 'POST',
      body: formData,
      headers: {},
      timeoutMs: 120000,
    });
    if (data && data.error) {
      showToast(data.error, 5000, 'error');
    } else if (data && (data.extract_error || (Array.isArray(data.files) && data.files.some(function(f){return f && f.extract_error;})))) {
      // Archive was rejected (zip-slip / zip-bomb / corrupt / too-many-members):
      // the file uploaded but extraction failed. Surface it as an error instead
      // of a misleading "Uploaded" success toast.
      var msg = data.extract_error
        || (data.files.find(function(f){return f && f.extract_error;}) || {}).extract_error
        || 'Archive extraction failed';
      showToast(msg, 5000, 'error');
    } else {
      showToast(t('uploaded') || ('Uploaded ' + (data.filename || file.name)), 2000);
    }
  } catch (e) {
    showToast(t('upload_failed') || ('Upload failed: ' + e.message), 5000, 'error');
  }
}

function _isOsFilesDrag(e) {
  return !!(e.dataTransfer && e.dataTransfer.types && e.dataTransfer.types.includes('Files'));
}

function _joinWorkspacePath(base, rel) {
  const b = base || '.';
  const r = (rel || '').replace(/^\/+|\/+$/g, '');
  if (!r) return b;
  return b === '.' ? r : `${b}/${r}`;
}

function _targetDirForRelDir(destDir, relDir) {
  const dirPart = (relDir || '').replace(/\/+$/, '');
  if (!dirPart) return destDir || '.';
  return _joinWorkspacePath(destDir, dirPart);
}

async function _readAllDirectoryEntries(reader) {
  const entries = [];
  while (true) {
    const batch = await new Promise((resolve, reject) => {
      reader.readEntries(resolve, reject);
    });
    if (!batch.length) break;
    entries.push(...batch);
  }
  return entries;
}

async function _collectFilesFromEntry(entry, relPrefix) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => {
      entry.file(resolve, reject);
    });
    return [{ file, relDir: relPrefix || '' }];
  }
  if (!entry.isDirectory) return [];
  const reader = entry.createReader();
  const children = await _readAllDirectoryEntries(reader);
  const dirPrefix = `${relPrefix || ''}${entry.name}/`;
  let out = [];
  for (const child of children) {
    out = out.concat(await _collectFilesFromEntry(child, dirPrefix));
  }
  return out;
}

async function _collectOsDropUploads(dataTransfer) {
  const out = [];
  const items = dataTransfer.items ? [...dataTransfer.items] : [];
  if (items.length && typeof items[0].webkitGetAsEntry === 'function') {
    for (const item of items) {
      if (item.kind !== 'file') continue;
      const entry = item.webkitGetAsEntry();
      if (!entry) continue;
      out.push(...await _collectFilesFromEntry(entry, ''));
    }
    if (out.length) return out;
  }
  for (const file of dataTransfer.files) {
    out.push({ file, relDir: '' });
  }
  return out;
}

async function uploadOsDropToWorkspace(dataTransfer, destDir) {
  if (!S.session || !dataTransfer) return;
  if(_workspacePathIsReadOnly(destDir || '.')){
    showToast(t('external_link_read_only'), 2000);
    return;
  }
  const uploads = await _collectOsDropUploads(dataTransfer);
  for (const { file, relDir } of uploads) {
    await uploadToWorkspace(file, _targetDirForRelDir(destDir, relDir));
  }
  if (S.session) await loadDir(S.currentDir);
}

function _clearWorkspaceOsUploadDragOver() {
  document.querySelectorAll('.file-item.drag-over-upload,.breadcrumb-seg.drag-over-upload').forEach((el) => {
    el.classList.remove('drag-over-upload');
  });
}

function _bindWorkspaceOsUploadDropTarget(el, destDir) {
  // Use addEventListener (not on-property assignment) so these OS-upload
  // handlers COMPOSE with the workspace tree-MOVE handlers bound by
  // _bindWorkspaceMoveDropTarget() on the same element. A property assignment
  // for the drop handler here would overwrite the move handler, and a
  // workspace-file drag would fall through to the document drop (inserting
  // @path into the composer) instead of moving the file. Each handler gates on
  // its own drag type (_isOsFilesDrag vs _isWorkspaceTreeMoveDrag), so only the
  // matching one acts.
  el.addEventListener('dragenter', (e) => {
    if (!_isOsFilesDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    el.classList.add('drag-over-upload');
  });
  el.addEventListener('dragover', (e) => {
    if (!_isOsFilesDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'copy';
    el.classList.add('drag-over-upload');
  });
  el.addEventListener('dragleave', (e) => {
    if (el.contains(e.relatedTarget)) return;
    el.classList.remove('drag-over-upload');
  });
  el.addEventListener('drop', async (e) => {
    if (!_isOsFilesDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    el.classList.remove('drag-over-upload');
    if(_workspacePathIsReadOnly(destDir || '.')){
      showToast(t('external_link_read_only'), 2000);
      return;
    }
    await uploadOsDropToWorkspace(e.dataTransfer, destDir);
  });
}

// Drag-and-drop files onto workspace file tree
if (typeof document !== 'undefined') {
  const _wsUploadInit = () => {
    const tree = $('fileTree');
    if (!tree) return;
    tree.addEventListener('dragenter', (e) => {
      if (e.dataTransfer && e.dataTransfer.types && e.dataTransfer.types.includes('Files')) {
        e.preventDefault();
        e.stopPropagation();
      }
    });
    tree.addEventListener('dragover', (e) => {
      if (e.dataTransfer && e.dataTransfer.types && e.dataTransfer.types.includes('Files')) {
        e.preventDefault();
        e.stopPropagation();
        if (e.target.closest('.file-item[data-ws-type="dir"],.file-item[data-ws-is-dir="true"],.breadcrumb-seg')) return;
        e.dataTransfer.dropEffect = 'copy';
        tree.classList.add('drag-over-upload');
      }
    });
    tree.addEventListener('dragleave', (e) => {
      if (tree.contains(e.relatedTarget)) return;
      tree.classList.remove('drag-over-upload');
    });
    tree.addEventListener('drop', async (e) => {
      tree.classList.remove('drag-over-upload');
      if (!e.dataTransfer || !e.dataTransfer.types || !e.dataTransfer.types.includes('Files')) return;
      if (e.target.closest('.file-item[data-ws-type="dir"],.file-item[data-ws-is-dir="true"],.breadcrumb-seg')) return;
      e.preventDefault();
      e.stopPropagation();
      if(_workspacePathIsReadOnly(S.currentDir || '.')){
        showToast(t('external_link_read_only'), 2000);
        return;
      }
      await uploadOsDropToWorkspace(e.dataTransfer, S.currentDir || '.');
    });
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _wsUploadInit, {once: true});
  } else {
    _wsUploadInit();
  }
}

// ============================================================================
// #2668 — Workspace Git/Changes controls (CORE, OPT-IN, DEFAULT OFF).
// Author: stocky789. Ported as a self-contained ADDITIVE block on top of
// master so the file is byte-identical to master's behaviour when the feature
// is off. The load-bearing default-off contract:
//   * RENDER gate: _applyWorkspaceGitVisibility() hides the Changes tab, git
//     badge-derived UI and branch control unless window._workspaceGitEnabled
//     === true. switchWorkspacePanelTab('changes') and renderGitBadge() also
//     no-op the Changes tab when the flag is off.
//   * POLL gate: the auto-refresh interval is installed by
//     _installWorkspaceGitAutoRefresh(), which is ONLY called from
//     _applyWorkspaceGitVisibility() when the flag is true (never at module
//     load). When off there is NO git poll and NO /api/git network traffic.
//   * Markup is hidden by default in index.html and only revealed here.
// Backend routes (/api/git/*) already exist in core master.
// ============================================================================

const GIT_AUTO_REFRESH_MS = 5000;

function _ensureGitState(){
  const scopeKey=`${(S.session&&S.session.session_id)||''}\n${(S.session&&S.session.workspace)||''}`;
  if(!S.git)S.git={status:null,selectedTab:'files',selectedDiff:null,loading:false,syncing:null,generatingCommitMessage:false,mutating:false,selectedPaths:new Set(),selectionTouched:false,selectionKey:scopeKey};
  if(typeof S.git.mutating==='undefined')S.git.mutating=false;
  if(typeof S.git.branchLoading==='undefined')S.git.branchLoading=false;
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
  if(!window._workspaceGitEnabled||!git.status||!git.status.is_git){
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
  if(!window._workspaceGitEnabled)return;
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
    const data=await api('/api/git/stash-checkout',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id,
      ref,
      mode:mode==='remote'?'remote':mode==='new'?'new':'local',
      new_branch:mode==='new'?ref:null,
      track:mode==='remote',
      dirty_mode:'block'
    })});
    _setGitStatus(data.git);
    git.branches=data.branches||null;
    const bits=[];
    if(data.stashed)bits.push(t('git_stashed_local_changes')||'saved local changes');
    if(data.restored_stash)bits.push(t('git_restored_local_changes')||'restored local changes');
    const note=bits.length?` · ${bits.join(', ')}`:'';
    showToast(data.current_branch?`${t('git_checked_out')||'Checked out'} ${data.current_branch}${note}`:`${t('git_checked_out')||'Checked out'}${note}`,4200);
    if(data.restore_failed){
      const stashRef=(data.restore_stash&&data.restore_stash.ref)||'';
      const detail=data.restore_error?`: ${data.restore_error}`:'';
      showToast(`${t('git_restore_failed')||'Could not restore local changes'}${stashRef?` (${stashRef})`:''}${detail}`,8000,'error');
    }
    // master lacks _closePreviewSurface: just hide the preview surface.
    $('previewArea').classList.remove('visible');
    await loadDir('.');
    await refreshGitBranches();
  }catch(e){
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
  if(!badge)return;
  const changesTab=$('btnWorkspaceChangesTab');
  if(!window._workspaceGitEnabled||!status||!status.is_git){
    // When the feature is off we never touch the legacy badge here — the
    // legacy /api/git-info badge in _refreshGitBadge() owns it. Only hide the
    // Changes tab and branch control (which only exist for this feature).
    if(changesTab)changesTab.hidden=true;
    renderGitBranchControl();
    renderWorkspacePanelTabState();
    return;
  }
  if(changesTab)changesTab.hidden=false;
  const totals=status.totals||{};
  let text=status.branch||'git';
  if((totals.changed||0)>0) text+=` \u00b7 ${totals.changed}\u2206`;
  if(status.behind>0) text+=` \u2193${status.behind}`;
  if(status.ahead>0) text+=` \u2191${status.ahead}`;
  badge.textContent=text;
  badge.className='git-badge'+((totals.changed||0)>0?' dirty':'');
  badge.style.display='';
  renderGitBranchControl();
  if(changesTab){
    changesTab.textContent=(totals.changed||0)>0?`${t('git_changes')} ${totals.changed}`:t('git_changes');
  }
  renderWorkspacePanelTabState();
}

async function refreshGitStatus(opts={}){
  if(!window._workspaceGitEnabled)return null;
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
    const sessionId=S.session.session_id;
    const priorSignature=_gitStatusSignature(git.status);
    const data=await api(`/api/git/status?session_id=${encodeURIComponent(sessionId)}`);
    if(!S.session||S.session.session_id!==sessionId)return git.status;
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
    renderWorkspacePanelTabState();
  }
}

function renderWorkspacePanelTabState(){
  if(!window._workspaceGitEnabled)return;
  const git=_ensureGitState();
  const hasGit=!!(git.status&&git.status.is_git);
  if(_workspacePanelActiveTab==='changes'&&!hasGit)_workspacePanelActiveTab='files';
  const active=_workspacePanelActiveTab==='artifacts'?'artifacts':(_workspacePanelActiveTab==='changes'?'changes':_workspacePanelActiveTab==='todos'?'todos':'files');
  git.selectedTab=active==='changes'?'changes':'files';
  _setWorkspacePanelTabDataset();
  const filesTab=$('workspaceFilesTab');
  const changesTab=$('btnWorkspaceChangesTab');
  const artifactsTab=$('workspaceArtifactsTab');
  const todosTab=$('workspaceTodosTab');
  const changesView=$('gitChangesView'), fileTree=$('fileTree'), emptyEl=$('wsEmptyState'), artifacts=$('workspaceArtifacts'), todosPanel=$('workspaceTodosPanel');
  const previewArea=$('previewArea');
  const previewVisible=previewArea&&previewArea.classList.contains('visible');
  if(changesTab)changesTab.hidden=!hasGit;
  const setTab=(el,isActive)=>{
    if(!el)return;
    el.classList.toggle('active',isActive);
    el.setAttribute('aria-selected',isActive?'true':'false');
  };
  setTab(filesTab,active==='files');
  setTab(changesTab,active==='changes');
  setTab(artifactsTab,active==='artifacts');
  setTab(todosTab,active==='todos');
  if(artifacts)artifacts.hidden=active!=='artifacts';
  if(todosPanel)todosPanel.hidden=active!=='todos';
  if(active==='artifacts'){
    if(fileTree)fileTree.style.display='none';
    if(emptyEl)emptyEl.style.display='none';
    if(changesView)changesView.style.display='none';
    if(previewArea)previewArea.classList.remove('visible');
    renderSessionArtifacts();
  }else if(active==='todos'){
    if(fileTree)fileTree.style.display='none';
    if(emptyEl)emptyEl.style.display='none';
    if(changesView)changesView.style.display='none';
    if(previewArea)previewArea.classList.remove('visible');
    _loadWorkspacePanelTodos();
  }else if(active==='changes'){
    if(fileTree)fileTree.style.display='none';
    if(emptyEl)emptyEl.style.display='none';
    if(changesView)changesView.style.display=previewVisible?'none':'flex';
  }else{
    if(changesView)changesView.style.display='none';
    if(fileTree&&!previewVisible)fileTree.style.display='';
  }
}

function _gitFiles(){
  const status=_ensureGitState().status;
  return (status&&Array.isArray(status.files))?status.files:[];
}

function _gitStatusForPath(path){
  if(!window._workspaceGitEnabled)return null;
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
    open.onclick=e=>{e.stopPropagation();openFile(file.path);};
    actions.appendChild(open);
  }
  if(actions.childNodes.length){
    row.classList.add('has-actions');
    row.appendChild(actions);
  }
  return row;
}

function renderGitChanges(){
  if(!window._workspaceGitEnabled)return;
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

async function openGitDiff(path,kind='unstaged'){
  if(!S.session||!window._workspaceGitEnabled)return;
  const git=_ensureGitState();
  _workspacePanelActiveTab='changes';
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
  open.onclick=()=>openFile(diff.path);
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
  }catch(e){showToast(e.message||`${t('git_stage')} failed`,3000,'error');}
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
  }catch(e){showToast(e.message||`${t('git_stage_all')} failed`,3000,'error');}
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
  }catch(e){showToast(e.message||`${t('git_stage_selected')} failed`,3000,'error');}
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

async function returnFromPreview(){
  if(typeof _previewDirty!=='undefined'&&_previewDirty){
    const ok=await showConfirmDialog({title:t('unsaved_confirm'),message:'',confirmLabel:t('discard'),danger:true,focusCancel:true});
    if(!ok)return;
  }
  if(_previewReturnTarget==='changes'&&window._workspaceGitEnabled){
    if(S.git){
      _workspacePanelActiveTab='changes';
      S.git.selectedTab='changes';
      S.git.selectedDiff=null;
    }
    // master lacks _closePreviewSurface: hide the preview surface and reset.
    const pa=$('previewArea');if(pa)pa.classList.remove('visible');
    const gdv=$('gitDiffView');if(gdv){gdv.style.display='none';gdv.innerHTML='';}
    const back=$('btnPreviewBack');if(back)back.style.display='none';
    _previewCurrentPath='';_previewCurrentMode='';_previewDirty=false;
    renderGitChanges();
    renderWorkspacePanelTabState();
    return;
  }
  const back=$('btnPreviewBack');if(back)back.style.display='none';
  if(typeof clearPreview==='function')clearPreview({keepPanelOpen:true});
}

// Outside-click / Escape guards for the branch menu (idempotent install).
function _installWorkspaceInteractionGuards(){
  if(window.__hermesWorkspaceInteractionGuardsInstalled)return;
  window.__hermesWorkspaceInteractionGuardsInstalled=true;
  document.addEventListener('click',event=>{
    if(!window._workspaceGitEnabled)return;
    const git=_ensureGitState();
    if(!git.branchMenuOpen)return;
    const control=$('gitBranchControl');
    if(control&&control.contains(event.target))return;
    closeGitBranchMenu();
  });
  document.addEventListener('keydown',event=>{
    if(event.key!=='Escape')return;
    if(!window._workspaceGitEnabled)return;
    const git=_ensureGitState();
    if(!git.branchMenuOpen)return;
    event.preventDefault();
    closeGitBranchMenu();
    const btn=$('btnGitBranchMenu');
    if(btn)btn.focus();
  });
}

async function _autoRefreshWorkspaceGitStatus(){
  if(!window._workspaceGitEnabled)return;
  if(!S.session)return;
  if(!_workspacePanelOpenForAutoRefresh())return;
  const git=_ensureGitState();
  if(git.mutating||git.syncing||git.generatingCommitMessage||git.branchMenuOpen)return;
  if(typeof _previewDirty!=='undefined'&&_previewDirty)return;
  await refreshGitStatus({auto:true,refreshBranches:false});
}

// POLL GATE: this installs the only background git poll. It is invoked solely
// from _applyWorkspaceGitVisibility() when window._workspaceGitEnabled===true,
// and never at module load — so with the feature off there is no interval, no
// visibilitychange listener, and zero /api/git network traffic.
function _installWorkspaceGitAutoRefresh(){
  if(window.__hermesWorkspaceGitAutoRefreshInstalled)return;
  if(!window._workspaceGitEnabled)return;
  window.__hermesWorkspaceGitAutoRefreshInstalled=true;
  window.setInterval(()=>{_autoRefreshWorkspaceGitStatus();},GIT_AUTO_REFRESH_MS);
  document.addEventListener('visibilitychange',()=>{
    if(document.visibilityState==='visible')_autoRefreshWorkspaceGitStatus();
  });
}

// RENDER GATE + POLL GATE entry point. Called from boot.js/panels.js whenever
// settings load or the toggle changes. When the flag is false this hides every
// git surface (Changes tab, branch control, changes view, diff view) and the
// Workspace panel shows ONLY the Files tab exactly as master does. When true it
// reveals the gated surfaces, installs the poll, and kicks an initial refresh.
function _applyWorkspaceGitVisibility(){
  const changesTab=$('btnWorkspaceChangesTab');
  const control=$('gitBranchControl');
  const changesView=$('gitChangesView');
  const diffView=$('gitDiffView');
  if(!window._workspaceGitEnabled){
    if(changesTab){changesTab.hidden=true;changesTab.classList.remove('active');changesTab.setAttribute('aria-selected','false');}
    if(control){control.style.display='none';control.classList.remove('is-open');}
    const menu=$('gitBranchMenu'); if(menu)menu.hidden=true;
    if(changesView)changesView.style.display='none';
    if(diffView){diffView.style.display='none';diffView.innerHTML='';}
    if(S.git){S.git.branchMenuOpen=false;S.git.selectedDiff=null;}
    // If we were sitting on the Changes tab, fall back to Files.
    if(_workspacePanelActiveTab==='changes'){
      _workspacePanelActiveTab='files';
      if(typeof switchWorkspacePanelTab==='function')switchWorkspacePanelTab('files');
    }
    return;
  }
  _installWorkspaceInteractionGuards();
  _installWorkspaceGitAutoRefresh();
  if(S.session)refreshGitStatus();
  else{renderGitBadge(null);renderWorkspacePanelTabState();}
}

_installWorkspaceInteractionGuards();
