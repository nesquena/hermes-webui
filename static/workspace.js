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
            // #5578: if we're ALREADY on the login page, appending
            // window.location.pathname+search (which contains ?next=…) into a
            // fresh next= wraps the login URL into itself and re-encodes it —
            // exponential URL growth on each expired-auth bounce until the tab
            // breaks. On the login page, just reload login WITHOUT a next (the
            // page preserves its own inner next); elsewhere, capture the path.
            if(redirect401){
              // Already on the login page? Reload login WITHOUT a next.
              const _p=(window.location.pathname||'').replace(/\/+$/,'');
              if(/(?:^|\/)login$/.test(_p)){
                window.location.href='login';
              }else{
                window.location.href='login?next='+encodeURIComponent(window.location.pathname+window.location.search);
              }
            }
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
  // Resolve the app-relative "/api/…" route against document.baseURI so the
  // URLs that are consumed OUTSIDE api() — previewImg.src, the media/pdf/html
  // frame src, the download anchor, window.open — keep working under a subpath
  // mount like /hermes/. A bare "/api/…" string resolves to the server root
  // there and 404s. (api() strips the leading slash and re-resolves against
  // baseURI itself, so routes passed through it are unaffected by already
  // being absolute.)
  const route=_workspaceRouteForPathRel(path, kind, opts);
  if(!route) return route;
  // Non-browser test harnesses have no document/location: keep the app-relative form.
  const base=(typeof document!=='undefined'&&document.baseURI)||(typeof location!=='undefined'&&location.href)||'';
  if(!base||!/^https?:\/\//i.test(base)) return route;
  const rel=route.startsWith('/') ? route.slice(1) : route;
  return new URL(rel, base).href;
}

function _workspaceRouteForPathRel(path, kind, opts={}){
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
let _workspaceTodosLastRenderedHash = null;
const _workspaceArtifactDisclosureState = Object.create(null);

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

function _workspaceTodosHash(items){
  if(!Array.isArray(items)) return '';
  let h=items.length+'|';
  for(let i=0;i<items.length;i++){
    const t=items[i]||{};
    h+=String(t.id==null?'':t.id)+'\x1f'+String(t.content==null?(t.text==null?'':t.text):t.content)+'\x1f'+String(t.status==null?'':t.status)+'\x1e';
  }
  return h;
}

function _workspaceTodosTabIsActive(){
  if(typeof window==='undefined'||window._workspaceTodosTab!==true) return false;
  if(typeof document==='undefined') return false;
  const rightPanel=document.querySelector('.rightpanel');
  if(!rightPanel||!rightPanel.dataset||rightPanel.dataset.activeTab!=='todos') return false;
  const tab=document.getElementById('workspaceTodosTab');
  const panel=document.getElementById('workspaceTodosPanel');
  return !!(tab&&panel&&!tab.hidden&&!panel.hidden);
}

function _resetWorkspaceTodosRenderCache(){
  _workspaceTodosLastRenderedHash=null;
}

function _refreshWorkspacePanelTodos(){
  if(!_workspaceTodosTabIsActive()) return;
  _loadWorkspacePanelTodos();
}

if(typeof document !== 'undefined'){
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _setWorkspacePanelTabDataset, {once:true});
  else _setWorkspacePanelTabDataset();
}

function switchWorkspacePanelTab(tab){
  _workspacePanelActiveTab = tab === 'artifacts' ? 'artifacts' : tab === 'todos' ? 'todos' : 'files';
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
  const artifacts = $('workspaceArtifacts');
  if(artifacts) artifacts.hidden = _workspacePanelActiveTab !== 'artifacts';
  const todosPanel = $('workspaceTodosPanel');
  if(todosPanel) todosPanel.hidden = _workspacePanelActiveTab !== 'todos';
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
    panel.innerHTML = renderTodoEmptyState({centered:true});
    return;
  }
  panel.innerHTML = renderTodoRows(todos, {metadata:true});
}

function _escHtml(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

const ARTIFACT_IGNORE_RE = /(^|\/)(?:\.git|\.hg|\.svn|node_modules|\.venv|venv|__pycache__|dist|build|\.next|\.cache)(?:\/|$)/;
// Canonical Hermes mutators plus MCP filesystem aliases that can create/edit files.
const ARTIFACT_MUTATION_TOOLS = new Set(['write_file','patch','edit_file','create_file','mcp_filesystem_write_file','mcp_filesystem_edit_file']);
const ARTIFACT_READ_TOOLS = new Set(['read_file','search_files']);
const ARTIFACT_WEB_TOOLS = new Set(['web_search','web_extract','browser_navigate']);
const ARTIFACT_CATEGORY_ORDER = Object.freeze(['modified','read','web','media']);
const ARTIFACT_CATEGORY_LIMITS = Object.freeze({modified:50, read:50, web:50, media:50});

function _normalizeArtifactPath(path, allowExtensionless=false){
  if(typeof path !== 'string' || !path) return '';
  path = String(path).trim().replace(/[\`"'<>),.;:]+$/g,'').replace(/^[\`"'(<]+/g,'').replace(/\\/g,'/');
  if(!path || path.length > 240 || path.includes('://')) return '';
  if(/^[a-z][a-z0-9+.-]*:/i.test(path) && !/^[a-z]:[\\/]/i.test(path)) return '';
  // Canonicalize workspace-relative prefixes so a file-tree open ("foo.md") and a
  // tool arg recorded as "./foo.md" or "~/foo.md" compare equal for mutation
  // tracking; otherwise an agent edit via a ./-prefixed path leaves the open
  // preview stale (#3262 / pre-release regression-gate finding).
  path = path.replace(/^~\//,'').replace(/^(?:\.\/)+/,'');
  if(!path || path === '.' || path.split(/[\\/]/).some(part => part === '..')) return '';
  if(/[\u0000-\u001f\u007f]/.test(path)) return '';
  if(ARTIFACT_IGNORE_RE.test(path)) return '';
  if(!allowExtensionless && !/[./]/.test(path)) return '';
  return path;
}

function _normalizeArtifactUrl(url){
  if(typeof url !== 'string') return '';
  const candidate = url.trim().replace(/^[`"'(<]+|[`"'\]>,.;:]+$/g,'');
  if(!candidate || candidate.length > 2048) return '';
  try{
    const parsed = new URL(candidate);
    if(!['http:','https:'].includes(parsed.protocol)||!parsed.hostname||parsed.username||parsed.password) return '';
    return parsed.href;
  }catch(_){
    return '';
  }
}

function _normalizeArtifactTarget(value, allowExtensionless=false){
  if(typeof value !== 'string') return '';
  const candidate = value.trim();
  if(/^[a-z][a-z0-9+.-]*:/i.test(candidate) && !/^[a-z]:[\\/]/i.test(candidate) && !/^(?:https?):/i.test(candidate)) return '';
  return /^(?:https?):/i.test(candidate) ? _normalizeArtifactUrl(candidate) : _normalizeArtifactPath(candidate, allowExtensionless);
}

function _normalizeArtifactFilePath(value){
  if(typeof value !== 'string') return '';
  const candidate = value.trim().replace(/^[`"'(<]+|[`"'\]>,.;:]+$/g,'');
  if(!/^file:\/\//i.test(candidate)) return '';
  const raw = candidate.replace(/^file:\/\//i,'');
  let localPath = '';
  const drive = raw.match(/^\/?([a-z]:)(?:[\\/]|$)(.*)$/i);
  if(drive){
    localPath = `${drive[1]}/${drive[2]}`;
  }else{
    try{
      const parsed = new URL(candidate);
      if(parsed.hostname) return '';
      localPath = parsed.pathname || '';
    }catch(_){
      localPath = raw;
    }
  }
  try{ localPath = decodeURIComponent(localPath); }catch(_){ return ''; }
  localPath = localPath.replace(/^\/([a-z]:[\\/])/i,'$1');
  return _normalizeArtifactPath(localPath, true);
}

function _normalizeArtifactMediaRef(value){
  if(typeof value !== 'string') return '';
  const candidate = value.trim().replace(/^[`"'(<]+|[`"'\]>,.;:]+$/g,'');
  if(!candidate) return '';
  if(/^data:image\/[a-z0-9.+-]+;base64,[a-z0-9+/=_-]+$/i.test(candidate)) return candidate;
  if(/^file:\/\//i.test(candidate)){
    const localPath = _normalizeArtifactFilePath(candidate);
    if(!localPath) return '';
    return `file://${localPath.startsWith('/') ? '' : '/'}${localPath}`;
  }
  return _normalizeArtifactTarget(candidate, true);
}

function _normalizeArtifactWorkspacePath(value, allowExtensionless=true){
  const path = _normalizeArtifactPath(value, allowExtensionless);
  if(!path) return '';
  const workspace = typeof S !== 'undefined' && S.session
    ? _normalizeArtifactPath(S.session.workspace, true)
    : '';
  if(!workspace) return path;
  const caseInsensitive = /^[a-z]:\//i.test(workspace);
  const comparablePath = caseInsensitive ? path.toLowerCase() : path;
  const comparableWorkspace = caseInsensitive ? workspace.toLowerCase() : workspace;
  if(comparablePath === comparableWorkspace) return '.';
  const prefix = `${comparableWorkspace}/`;
  return comparablePath.startsWith(prefix) ? path.slice(workspace.length + 1) : path;
}

function _normalizeArtifactOpenPath(value){
  if(typeof value !== 'string') return '';
  let path = value.trim().replace(/\\/g, '/');
  path = path.replace(/^~\//,'').replace(/^\.\/+/,'');
  const workspace = typeof S !== 'undefined' && S.session && typeof S.session.workspace === 'string'
    ? S.session.workspace.replace(/\\/g, '/').replace(/\/+$/,'')
    : '';
  if(workspace){
    const caseInsensitive = /^[a-z]:\//i.test(workspace);
    const comparablePath = caseInsensitive ? path.toLowerCase() : path;
    const comparableWorkspace = caseInsensitive ? workspace.toLowerCase() : workspace;
    if(comparablePath === comparableWorkspace) return '.';
    const prefix = `${comparableWorkspace}/`;
    if(comparablePath.startsWith(prefix)) path = path.slice(workspace.length + 1);
  }
  return path || '.';
}

function _parseArtifactJson(value){
  if(value && typeof value === 'object') return value;
  if(typeof value !== 'string') return null;
  const text = value.trim();
  if(!text) return null;
  const attempts = [text];
  const end = Math.max(text.lastIndexOf(String.fromCharCode(125)), text.lastIndexOf(String.fromCharCode(93)));
  if(end >= 0 && end + 1 < text.length) attempts.push(text.slice(0, end + 1));
  for(const candidate of attempts){
    try{
      let parsed = JSON.parse(candidate);
      if(typeof parsed === 'string') parsed = JSON.parse(parsed);
      if(parsed && typeof parsed === 'object') return parsed;
    }catch(_){ }
  }
  return null;
}

function _artifactToolId(tool){
  if(!tool || typeof tool !== 'object') return '';
  return String(tool.tool_call_id || tool.tool_use_id || tool.call_id || tool.tid || tool.id || '').trim();
}

function _artifactToolName(tool){
  if(!tool || typeof tool !== 'object') return '';
  const fn = tool.function && typeof tool.function === 'object' ? tool.function : {};
  return String(tool.name || tool.tool_name || fn.name || '').replace(/^functions\./,'');
}

function _artifactToolArgs(tool){
  if(!tool || typeof tool !== 'object') return {};
  const fn = tool.function && typeof tool.function === 'object' ? tool.function : {};
  let args = tool.arguments || tool.args || tool.input || fn.arguments || fn.input || {};
  if(typeof args === 'string'){
    try{ args = JSON.parse(args); }catch(_){ }
  }
  return args;
}

function _artifactResultValues(tool){
  if(!tool || typeof tool !== 'object') return [];
  return [tool.result, tool.output, tool.content, tool.snippet, tool.preview]
    .filter(value => value != null && value !== '');
}

function _artifactTextFromValue(value){
  if(typeof value === 'string') return value;
  if(Array.isArray(value)) return value.map(item => _artifactTextFromValue(item)).filter(Boolean).join('\n');
  if(value && typeof value === 'object'){
    if(typeof value.text === 'string') return value.text;
    if(value.content != null) return _artifactTextFromValue(value.content);
  }
  return '';
}

function _artifactPartialFieldValues(value, fields){
  const text = _artifactTextFromValue(value);
  if(!text) return [];
  const names = fields.join('|');
  const re = new RegExp(`(?:["'](?:${names})["']|(?:${names}))\\s*:\\s*["']((?:\\\\.|[^"'])*)`, 'gi');
  const out = [];
  let match;
  while((match = re.exec(text))){
    let decoded = match[1];
    try{ decoded = JSON.parse(`"${decoded}"`); }catch(_){ }
    decoded = String(decoded).replace(/\\\\/g,'\\');
    out.push(decoded);
  }
  return out;
}

function _artifactCandidatesFromText(text){
  if(!text || typeof text !== 'string') return [];
  const out = [];
  const seen = new Set();
  const add = (value, category='modified', kind='diff') => {
    const path = category === 'web' ? _normalizeArtifactUrl(value) : category === 'media' ? _normalizeArtifactMediaRef(value) : _normalizeArtifactTarget(value, category === 'read');
    if(!path || seen.has(`${category}:${path}`)) return;
    seen.add(`${category}:${path}`); out.push({path, category, kind});
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
  const media = /MEDIA:([^\s\)\]]+)/g;
  while((m = media.exec(text))) add(m[1], 'media', 'media');
  return out;
}

function _artifactCandidatesFromToolCall(tc){
  if(!tc) return [];
  const name = _artifactToolName(tc);
  const args = _artifactToolArgs(tc);
  const resultValues = _artifactResultValues(tc);
  const out = [];
  const addPath = (path, category, source=name || 'tool') => {
    path = _normalizeArtifactPath(path, category === 'read');
    if(path) out.push({path, category, kind:source});
  };
  const addUrl = (url, source='web_page') => {
    url = _normalizeArtifactUrl(url);
    if(url) out.push({path:url, category:'web', kind:source});
  };
  const addPaths = (value, category, source) => {
    if(Array.isArray(value)) value.forEach(path => addPaths(path, category, source));
    else if(typeof value === 'string') addPath(value, category, source);
  };
  const addUrls = (value, source) => {
    if(Array.isArray(value)) value.forEach(url => addUrls(url, source));
    else if(typeof value === 'string') addUrl(value, source);
    else if(value && typeof value === 'object') addUrl(value.url || value.href, source);
  };
  const addResultUrls = value => {
    const parsed = _parseArtifactJson(value);
    if(parsed && typeof parsed === 'object'){
      addUrls(parsed.url, 'web_result');
      addUrls(parsed.urls, 'web_result');
      const web = parsed.data && typeof parsed.data === 'object' ? parsed.data.web : parsed.web;
      if(Array.isArray(web)) web.forEach(resultItem => {
        if(resultItem && typeof resultItem === 'object') addUrl(resultItem.url || resultItem.href, 'web_result');
      });
      if(Array.isArray(parsed.results)) parsed.results.forEach(resultItem => {
        if(resultItem && typeof resultItem === 'object') addUrl(resultItem.url || resultItem.href, 'web_result');
      });
    }
    for(const url of _artifactPartialFieldValues(value, ['url','href'])) addUrl(url, 'web_result');
  };
  if(ARTIFACT_MUTATION_TOOLS.has(name) && args && typeof args === 'object'){
    for(const key of ['path','file_path','source','destination']) addPath(args[key], 'modified');
    if(Array.isArray(args.paths)) args.paths.forEach(p=>addPath(p, 'modified'));
    if(Array.isArray(args.edits)) args.edits.forEach(e=>addPath(e&&e.path, 'modified'));
  }
  if(ARTIFACT_READ_TOOLS.has(name) && args && typeof args === 'object'){
    if(name === 'read_file'){
      for(const key of ['path','file_path']) addPath(args[key], 'read', name);
      addPaths(args.paths, 'read', name);
      for(const value of resultValues){
        const parsed = _parseArtifactJson(value);
        if(parsed && typeof parsed === 'object'){
          addPath(parsed.path, 'read', name);
          addPath(parsed.file_path, 'read', name);
        }
        for(const path of _artifactPartialFieldValues(value, ['path','file_path'])) addPath(path, 'read', name);
      }
    }else if(name === 'search_files'){
      const parsed = resultValues.map(_parseArtifactJson).find(value => value && typeof value === 'object');
      if(parsed && typeof parsed === 'object'){
        addPaths(parsed.files, 'read', name);
        if(Array.isArray(parsed.matches)) parsed.matches.forEach(match => addPath(match && match.path, 'read', name));
        if(typeof parsed.matches_text === 'string'){
          for(const line of parsed.matches_text.split(/\r?\n/)){
            if(line.trim() && !/^\s+\d+\s*:/.test(line) && !/^\s/.test(line)) addPath(line.trim(), 'read', name);
          }
        }
      }
      for(const path of resultValues.flatMap(value => _artifactPartialFieldValues(value, ['path','file_path']))) addPath(path, 'read', name);
    }
  }
  if(ARTIFACT_WEB_TOOLS.has(name) && args && typeof args === 'object'){
    addUrls(args.url, 'web_page');
    addUrls(args.urls, 'web_page');
    for(const value of resultValues) addResultUrls(value);
  }
  // Tool results may include unified diffs from patch-style tools; scan those
  // narrowly after structured args so diff headers can still contribute paths.
  for(const value of resultValues){
    for(const a of _artifactCandidatesFromText(_artifactTextFromValue(value))){
      if(a.category === 'modified') out.push(a);
    }
  }
  if(!out.length && ARTIFACT_MUTATION_TOOLS.has(name)){
    const argsText = typeof args === 'string' ? args : JSON.stringify(args || {});
    for(const a of _artifactCandidatesFromText(argsText)) out.push(a);
  }
  return out;
}

function _artifactToolResultPayload(message){
  if(!message || typeof message !== 'object') return null;
  const payload = block => ({
    result: block.content,
    output: block.output,
    snippet: block.snippet,
    preview: block.preview,
  });
  if(Array.isArray(message.content)){
    const results = message.content.filter(block => block && block.type === 'tool_result');
    if(results.length === 1) return payload(results[0]);
    if(results.length > 1) return results.map(payload);
  }
  if(message.role === 'tool') return payload(message);
  return null;
}

function _artifactToolResultsById(messages){
  const results = new Map();
  for(const message of (Array.isArray(messages) ? messages : [])){
    if(!message || typeof message !== 'object') continue;
    if(message.role === 'tool'){
      const id = _artifactToolId(message);
      if(id) results.set(id, _artifactToolResultPayload(message));
    }
    if(Array.isArray(message.content)){
      for(const block of message.content){
        if(!block || block.type !== 'tool_result') continue;
        const id = _artifactToolId({tool_use_id:block.tool_use_id, tool_call_id:block.tool_call_id});
        if(id) results.set(id, {result:block.content, output:block.output, snippet:block.snippet, preview:block.preview});
      }
    }
  }
  return results;
}

function _artifactToolSources(){
  const sources = [];
  if(typeof S !== 'undefined' && Array.isArray(S.toolCalls)) sources.push(S.toolCalls);
  if(typeof S !== 'undefined' && Array.isArray(S._settledLiveToolMetadata)) sources.push(S._settledLiveToolMetadata);
  if(typeof S !== 'undefined' && S.session && Array.isArray(S.session.tool_calls)) sources.push(S.session.tool_calls);
  return sources.flatMap(source => source);
}

const _turnMutatedPreviewPaths = new Set();

function resetTurnWorkspaceMutations(){
  _turnMutatedPreviewPaths.clear();
}

function noteWorkspaceMutationsFromToolCall(tc){
  for(const a of _artifactCandidatesFromToolCall(tc)){
    if(a.category && a.category !== 'modified') continue;
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
  const categoryOrder = ARTIFACT_CATEGORY_ORDER;
  const grouped = Object.fromEntries(categoryOrder.map(category => [category, []]));
  const seen = Object.fromEntries(categoryOrder.map(category => [category, new Set()]));
  const push = (candidate, source) => {
    const category = categoryOrder.includes(candidate.category) ? candidate.category : 'modified';
    const path = category === 'web' ? _normalizeArtifactUrl(candidate.path) : category === 'media' ? _normalizeArtifactMediaRef(candidate.path) : _normalizeArtifactTarget(candidate.path, category === 'read');
    if(!path || seen[category].has(path) || grouped[category].length >= ARTIFACT_CATEGORY_LIMITS[category]) return;
    seen[category].add(path);
    grouped[category].push({path, category, source: candidate.kind || source});
  };
  const toolResultsById = _artifactToolResultsById(S.messages);
  const processToolCall = (tc, source) => {
    if(!tc || typeof tc !== 'object') return;
    const result = toolResultsById.get(_artifactToolId(tc));
    const fakeTc = {...tc};
    if(result) Object.assign(fakeTc, Array.isArray(result) ? {result} : result);
    for(const a of _artifactCandidatesFromToolCall(fakeTc)) push(a, a.kind || _artifactToolName(tc) || source || 'tool');
  };
  // Session summaries remain authoritative when the visible message window is
  // truncated or _syncToolCallsForLoadedMessages clears the live projection.
  for(const tc of _artifactToolSources()){
    processToolCall(tc, 'tool_summary');
  }
  // Source 2 & 3: message-level data — both text-mined diffs and structured
  // tool_calls / tool_use content blocks that survive the S.toolCalls clear.
  for(const msg of (S.messages || [])){
    if(!msg) continue;
    const messageRole = String(msg.role || '').toLowerCase();
    const allowMessageMedia = messageRole === 'assistant';
    const textValues = [];
    if(typeof msg.content === 'string') textValues.push(msg.content);
    else if(Array.isArray(msg.content)) for(const block of msg.content){
      if(block && ['text','input_text','output_text'].includes(block.type)){
        const text = block.text || block.input_text || block.output_text || block.content;
        if(typeof text === 'string') textValues.push(text);
      }
    }
    else if(typeof msg.text === 'string') textValues.push(msg.text);
    else if(typeof msg.message === 'string') textValues.push(msg.message);
    // Text-mined diff/patch fences (existing path).
    for(const text of textValues){
      for(const a of _artifactCandidatesFromText(text)){
        if(a.category === 'media' && !allowMessageMedia) continue;
        push(a, a.kind);
      }
    }
    // Structured tool metadata is owned by assistant messages only.
    if(messageRole !== 'assistant') continue;
    // Structured tool_calls array (OpenAI format: {function:{name,arguments}}).
    for(const toolCalls of [msg.tool_calls, msg._partial_tool_calls]){
      if(!Array.isArray(toolCalls)) continue;
      for(const tc of toolCalls){
        if(!tc || typeof tc !== 'object') continue;
        processToolCall(tc, 'tool_call');
      }
    }
    // Structured content array with tool_use blocks (Anthropic format).
    if(Array.isArray(msg.content)){
      for(const block of msg.content){
        if(!block || block.type !== 'tool_use') continue;
        processToolCall(block, 'tool_use');
      }
    }
  }
  return categoryOrder.flatMap(category => grouped[category]);
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
  const displayPath = (p) => {
    if(/^https?:/i.test(p)) return p;
    const filePath = typeof _normalizeArtifactFilePath === 'function'
      ? _normalizeArtifactFilePath(p)
      : '';
    const rawPath = filePath || p;
    const normalized = typeof _normalizeArtifactWorkspacePath === 'function'
      ? _normalizeArtifactWorkspacePath(rawPath, true)
      : (() => {
        const fallbackPath = String(rawPath).replace(/\\/g, '/');
        const fallbackWorkspace = String(S.session && S.session.workspace || '')
          .replace(/\\/g, '/').replace(/\/+$/,'');
        if(!fallbackWorkspace) return fallbackPath;
        if(fallbackPath === fallbackWorkspace) return '.';
        return fallbackPath.startsWith(`${fallbackWorkspace}/`)
          ? fallbackPath.slice(fallbackWorkspace.length + 1)
          : fallbackPath;
      })();
    return normalized || p;
  };
  const splitArtifactDisplayPath = (path) => {
    const slash = path.lastIndexOf('/');
    if(slash < 0) return {name: path, head: '', tail: ''};
    const directory = path.slice(0, slash + 1);
    const parentSlash = directory.lastIndexOf('/', directory.length - 2);
    return {
      name: path.slice(slash + 1),
      head: directory.slice(0, parentSlash + 1),
      tail: directory.slice(parentSlash + 1),
    };
  };
  const categoryLabels = {
    modified: 'workspace_artifact_category_modified',
    read: 'workspace_artifact_category_read',
    web: 'workspace_artifact_category_web',
    media: 'workspace_artifact_category_media',
  };
  const sourceLabels = {
    diff: 'workspace_artifact_source_diff',
    write_file: 'workspace_artifact_source_write_file',
    patch: 'workspace_artifact_source_patch',
    edit_file: 'workspace_artifact_source_edit_file',
    create_file: 'workspace_artifact_source_create_file',
    read_file: 'workspace_artifact_source_read_file',
    search_files: 'workspace_artifact_source_search_files',
    web_page: 'workspace_artifact_source_web_page',
    web_result: 'workspace_artifact_source_web_result',
    media: 'workspace_artifact_source_media',
  };
  const categoryOrder = ARTIFACT_CATEGORY_ORDER;
  const categoryLabelFallbacks = {
    modified: 'Modified Files',
    read: 'Files Read',
    web: 'Web Pages',
    media: 'Inline Media',
  };
  const artifactMediaHref = (ref) => {
    if(/^data:image\/[a-z0-9.+-]+;base64,[a-z0-9+/=_-]+$/i.test(ref)) return ref;
    if(/^https?:/i.test(ref)) return _normalizeArtifactUrl(ref);
    const path = _normalizeArtifactFilePath(ref) || _normalizeArtifactTarget(ref, true);
    if(!path || !S.session || !S.session.session_id) return '';
    const workspace = _normalizeArtifactPath(S.session.workspace, true);
    const isAbsolute = path.startsWith('/') || /^[a-z]:\//i.test(path);
    const routePath = !isAbsolute && workspace ? `${workspace}/${path}` : path;
    return `api/media?path=${encodeURIComponent(routePath)}&session_id=${encodeURIComponent(S.session.session_id)}&inline=1`;
  };
  const renderItem = item => {
    const path = displayPath(item.path);
    const parts = splitArtifactDisplayPath(path);
    const directory = (parts.head || parts.tail)
      ? `<div class="workspace-artifact-directory"><span class="workspace-artifact-directory-head">${esc(parts.head)}</span><span class="workspace-artifact-directory-tail">${esc(parts.tail)}</span></div>`
      : '';
    const sourceKey = sourceLabels[item.source] || 'workspace_artifact_source_session';
    const sourceValue = t(sourceKey);
    const source = esc(sourceValue);
    const sourceAttrs = sourceKey ? ` data-i18n="${sourceKey}"` : '';
    const contents = `<div class="workspace-artifact-filename">${esc(parts.name)}</div>${directory}<div class="workspace-artifact-meta"${sourceAttrs}>${source}</div>`;
    if(item.category === 'web' || item.category === 'media'){
      const url = item.category === 'web' ? _normalizeArtifactUrl(item.path) : artifactMediaHref(item.path);
      return url ? `<a class="workspace-artifact-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="${esc(url)}">${contents}</a>` : '';
    }
    return `<button type="button" class="workspace-artifact-item" title="${esc(path)}" data-artifact-path="${esc(item.path)}" onclick="openArtifactPath(this.dataset.artifactPath)">${contents}</button>`;
  };
  root.innerHTML = categoryOrder.map(category => {
    const categoryItems = items.filter(item => (item.category || 'modified') === category);
    if(!categoryItems.length) return '';
    const labelKey = categoryLabels[category];
    const label = t(labelKey);
    const labelText = label === labelKey ? categoryLabelFallbacks[category] : label;
    return `<details class="workspace-artifact-group" data-artifact-category="${category}"><summary class="workspace-artifact-group-title"><span data-i18n="${labelKey}">${esc(labelText)}</span><span class="workspace-artifacts-count">${categoryItems.length}</span></summary><div class="workspace-artifact-group-items">${categoryItems.map(renderItem).join('')}</div></details>`;
  }).join('');
  for(const group of root.querySelectorAll('.workspace-artifact-group')){
    group.open = _workspaceArtifactDisclosureState[group.dataset.artifactCategory] !== false;
    group.addEventListener('toggle', () => {
      _workspaceArtifactDisclosureState[group.dataset.artifactCategory] = group.open;
    });
  }
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
  const rel = _normalizeArtifactOpenPath(path);
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
    if(data.workspace_recovered&&data.workspace){
      S.session.workspace=String(data.workspace);
      S._dirCache={};
      _restoreExpandedDirs();
      if(typeof syncWorkspaceDisplays==='function')syncWorkspaceDisplays();
      if(typeof syncTerminalButton==='function')syncTerminalButton();
      showToast(t('workspace_recovered_notice',S.session.workspace),5000,'warning');
    }
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
    if(!path||path==='.') _refreshGitBadge();
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
  '.doc','.xls','.ppt','.odt','.ods','.odp',
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
let _previewServerEditable = null;  // backend editability metadata when available
let _previewSaveRoute = '/api/file/save';  // current save adapter for the open preview
let _previewOfficeFormat = '';  // current claimed Office format, if any
let _previewPreviewKind = '';  // preview family returned by the backend

function showPreview(mode){
  // mode: 'code' | 'csv' | 'image' | 'md' | 'html' | 'pdf' | 'audio' | 'video'
  $('previewCode').style.display     = mode==='code'  ? '' : 'none';
  $('previewImgWrap').style.display  = mode==='image' ? '' : 'none';
  const mediaWrap=$('previewMediaWrap'); if(mediaWrap) mediaWrap.style.display = (mode==='audio'||mode==='video') ? '' : 'none';
  const pdfWrap=$('previewPdfWrap'); if(pdfWrap) pdfWrap.style.display = mode==='pdf' ? '' : 'none';
  $('previewMd').style.display       = (mode==='md'||mode==='csv') ? '' : 'none';
  $('previewHtmlWrap').style.display = mode==='html'  ? '' : 'none';
  $('previewEditArea').style.display = 'none';  // start in read-only
  const badge=$('previewBadge');
  badge.className='preview-badge '+mode;
  badge.textContent = mode==='image'?'image':mode==='audio'?'audio':mode==='video'?'video':mode==='pdf'?'pdf':mode==='csv'?'csv':mode==='md'?'md':mode==='html'?'html':fileExt($('previewPathText').textContent)||'text';
  _previewCurrentMode = mode;
  _previewDirty = false;
  updateEditBtn();
  // Show "Open in browser" button for iframe-backed document previews
  const openBtn=$('btnOpenInBrowser');
  if(openBtn) openBtn.style.display = (mode==='html'||mode==='pdf')?'inline-flex':'none';
  setLargeMarkdownForceRenderVisible(false);
}

function updateEditBtn(){
  const btn=$('btnEditFile');
  if(!btn)return;
  const editable = !_workspacePathIsReadOnly(_previewCurrentPath)
    && (_previewServerEditable===null
      ? (_previewCurrentMode==='code'||_previewCurrentMode==='md'||_previewCurrentMode==='csv')
      : !!_previewServerEditable);
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
  if(!editing && _previewServerEditable===false){
    showToast('This Office document is preview-only.', 3000, 'error');
    return;
  }
  if(editing){
    // Save
    if(!S.session||!_previewCurrentPath)return;
    const content=$('previewEditArea').value;
    try{
      const saved=await api(_previewSaveRoute||'/api/file/save',{method:'POST',body:JSON.stringify({
        session_id:S.session.session_id, path:_previewCurrentPath, content
      })});
      const savedContent=saved&&typeof saved.content==='string'?saved.content:content;
      if(saved && typeof saved.editable==='boolean') _previewServerEditable = saved.editable;
      if(saved && saved.preview_kind) _previewPreviewKind = saved.preview_kind;
      if(saved && saved.office_format) _previewOfficeFormat = saved.office_format;
      if(saved && saved.preview_kind==='office' && saved.office_format==='docx'){
        _previewSaveRoute = '/api/file/office-save';
      }
      _previewDirty=false;
      // Update read-only views AND the cached raw content so a later
      // "Render as markdown anyway" force-render reflects the just-saved text
      // (not the stale pre-edit fetch). #3378 review (Codex).
      _previewRawContent = savedContent;
      _previewRawContentPath = _previewCurrentPath;
      if(_previewCurrentMode==='code') $('previewCode').textContent=savedContent;
      else if(_previewCurrentMode==='csv') renderCsvPreviewContent(_previewCurrentPath, savedContent);
      else renderMarkdownPreviewContent({content:savedContent});
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

  _previewServerEditable = null;
  _previewSaveRoute = '/api/file/save';
  _previewOfficeFormat = '';
  _previewPreviewKind = '';

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
      if(data.preview_kind==='office'){
        _previewRawContent = data.content || '';
        _previewRawContentPath = path;
        _previewServerEditable = typeof data.editable === 'boolean' ? data.editable : null;
        _previewPreviewKind = data.preview_kind || '';
        _previewOfficeFormat = data.office_format || '';
        _previewSaveRoute = data.preview_kind==='office' ? '/api/file/office-save' : '/api/file/save';
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

async function copyPreviewRelativePath(){
  if(!_previewCurrentPath) return;
  const btn=$('btnCopyPreviewRelPath');
  if(btn&&btn.disabled) return;
  if(btn) btn.disabled=true;
  try{
    const rel=_normalizeWorkspaceRelPath(_previewCurrentPath)||_previewCurrentPath;
    if(typeof _copyTextWithFallback==='function'){
      await _copyTextWithFallback(rel,t('path_copied'),t('path_copy_failed'));
      return;
    }
    try{
      await navigator.clipboard.writeText(rel);
      showToast(t('path_copied'));
    }catch(clipErr){
      const ta=document.createElement('textarea');
      ta.value=rel;
      ta.style.cssText='position:fixed;left:-9999px;top:-9999px;';
      document.body.appendChild(ta);
      ta.select();
      let copied=false;
      try{copied=document.execCommand('copy');}catch(_){}
      ta.remove();
      if(copied) showToast(t('path_copied'));
      else showToast(t('path_copy_failed')+(clipErr&&clipErr.message?clipErr.message:String(clipErr)));
    }
  }catch(err){
    showToast(t('path_copy_failed')+(err.message||err));
  }finally{
    if(btn) btn.disabled=false;
  }
}

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
