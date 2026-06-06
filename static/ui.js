// `todos` is the single source of truth for the Todos panel.  Any update
// goes through the `todo_state` SSE event (live) or session.todo_state
// (cold-load).  `todoStateMeta` doubles as a sentinel: while it is null
// no explicit signal has been seen, so loadTodos() falls back to the
// legacy reverse-scan over S.messages — that keeps new clients working
// against old servers (Phase 1 may not yet be deployed everywhere).
// See api/todo_state.py for the wire contract.
const S={session:null,messages:[],entries:[],busy:false,pendingFiles:[],toolCalls:[],activeStreamId:null,currentDir:'.',activeProfile:'default',activeProfileIsDefault:true,showHiddenWorkspaceFiles:false,todos:[],todoStateMeta:null};

function assistantDisplayName(){
  if(S.activeProfile&&S.activeProfile!=='default') return S.activeProfile.charAt(0).toUpperCase()+S.activeProfile.slice(1);
  return window._botName||'Hermes';
}
const INFLIGHT={};  // keyed by session_id while request in-flight
const SESSION_QUEUES={};  // keyed by session_id for queued follow-up turns
const MAX_UPLOAD_BYTES=(window.__HERMES_CONFIG__&&window.__HERMES_CONFIG__.maxUploadBytes)||20*1024*1024;
const MAX_UPLOAD_MB=Math.round(MAX_UPLOAD_BYTES/1024/1024);
// Tracks which session's queue to drain in setBusy(false).
// Set to activeSid just before setBusy(false) in done/error handlers so the
// queue drains the session that *finished*, not the one currently viewed.
// Single-shot: setBusy() reads and clears this on every call. Concurrent
// back-to-back stream completions would overwrite it, but HTTPServer is
// single-threaded so only one done event fires at a time in practice.
let _queueDrainSid=null;
const $=id=>document.getElementById(id);
const OFFLINE_RECHECK_MS=2500;
let _offlineVisible=false;
let _offlineReason='browser';
let _offlineProbeTimer=null;
let _offlineChecking=false;
let _offlineProbePromise=null;
let _offlineHealthProbePromise=null;
let _offlineRawFetch=null;
let _offlineFetchPatched=false;
function _browserReportsOnline(){return !('onLine' in navigator)||navigator.onLine!==false;}
function _offlineHealthUrl(){const url=new URL('health',document.baseURI||location.href);url.searchParams.set('offline_probe',String(Date.now()));return url.href;}
function _setOfflineChecking(checking){
  _offlineChecking=!!checking;
  const btn=$('offlineCheckNow');
  if(btn){btn.disabled=_offlineChecking;btn.textContent=_offlineChecking?t('offline_checking'):t('offline_check_now');}
}
function _renderOfflineBanner(){
  const banner=$('offlineBanner');
  if(!banner)return;
  const detail=$('offlineDetails');
  if(detail)detail.textContent=t(_offlineReason==='browser'?'offline_browser_detail':'offline_network_detail');
  const title=$('offlineTitle');
  if(title)title.textContent=t('offline_title');
  const auto=$('offlineAutorefresh');
  if(auto)auto.textContent=t('offline_autorefresh');
  _setOfflineChecking(_offlineChecking);
  banner.hidden=false;
  banner.classList.add('visible');
}
function _startOfflineProbeTimer(){
  if(_offlineProbeTimer)return;
  _offlineProbeTimer=setInterval(()=>{checkOfflineRecoveryNow();},OFFLINE_RECHECK_MS);
}
function _stopOfflineProbeTimer(){
  if(_offlineProbeTimer){clearInterval(_offlineProbeTimer);_offlineProbeTimer=null;}
}
function showOfflineBanner(reason){
  _offlineVisible=true;
  _offlineReason=reason||(_browserReportsOnline()?'network':'browser');
  _renderOfflineBanner();
  _startOfflineProbeTimer();
}
function isOfflineBannerVisible(){return _offlineVisible;}
function _hideOfflineBanner(){
  _offlineVisible=false;
  _stopOfflineProbeTimer();
  _setOfflineChecking(false);
  const banner=$('offlineBanner');
  if(banner){banner.classList.remove('visible');banner.hidden=true;}
}
async function _probeOfflineRecovery(){
  if(_offlineHealthProbePromise)return _offlineHealthProbePromise;
  _offlineHealthProbePromise=(async()=>{
    const fetcher=_offlineRawFetch||window.fetch.bind(window);
    try{
      const res=await fetcher(_offlineHealthUrl(),{cache:'no-store',credentials:'include'});
      return !!(res&&res.ok);
    }catch(_){return false;}
  })();
  try{return await _offlineHealthProbePromise;}
  finally{_offlineHealthProbePromise=null;}
}
async function checkOfflineRecoveryNow(){
  if(_offlineProbePromise)return _offlineProbePromise;
  _offlineProbePromise=(async()=>{
    if(!_offlineVisible)return false;
    if(!_browserReportsOnline()){showOfflineBanner('browser');return false;}
    _setOfflineChecking(true);
    const ok=await _probeOfflineRecovery();
    _setOfflineChecking(false);
    if(ok){_stopOfflineProbeTimer();await _recoverFromOfflineSoftly();return true;}
    showOfflineBanner('network');
    return false;
  })();
  try{return await _offlineProbePromise;}
  finally{_offlineProbePromise=null;}
}
// Recover from a transient "Connection lost" without a full page reload.
//
// The offline banner fires whenever a fetch/SSE errors — which Android does
// aggressively every time the PWA is backgrounded, even for a second. The old
// behaviour here was `window.location.reload()`: a hard cold boot that re-runs
// the whole app and re-pulls /api/sessions + /api/session, producing the
// multi-second "reload to see the conversation I was just in" flash on every
// resume. The reload was also intermittent (only when a request actually
// errored that time), matching the reported "sometimes it reloads, sometimes
// it doesn't".
//
// The server keeps the agent running and buffers stream events while no
// subscriber is attached (#2307), so a hard reload is never required to
// recover — we just need to reattach. This does the soft path: hide the
// banner, restart the gateway SSE (bfcache/background kills the connection),
// and re-fetch the active session so any messages that landed while we were
// away appear. A full reload is the fallback only if the soft path throws.
async function _recoverFromOfflineSoftly(){
  try{
    _hideOfflineBanner();
    if(typeof startGatewaySSE==='function') startGatewaySSE();
    if(S.session && typeof refreshSession==='function'){
      await refreshSession();
    }
    return true;
  }catch(_){
    // Soft reattach failed (server mid-restart, session gone, etc.) — fall
    // back to the original hard reload so the user is never stuck offline.
    window.location.reload();
    return false;
  }
}
function _isAbortError(e){return !!(e&&(e.name==='AbortError'||e.code===20));}
function _patchOfflineFetch(){
  if(_offlineFetchPatched||typeof window.fetch!=='function')return;
  _offlineFetchPatched=true;
  _offlineRawFetch=window.fetch.bind(window);
  window.fetch=async function(...args){
    try{return await _offlineRawFetch(...args);}
    catch(e){
      if(!_browserReportsOnline())showOfflineBanner('browser');
      else if(e instanceof TypeError&&!_isAbortError(e))void _probeOfflineRecovery().then(ok=>{if(!ok)showOfflineBanner('network');});
      throw e;
    }
  };
}
function initOfflineMonitor(){
  _patchOfflineFetch();
  window.addEventListener('offline',()=>showOfflineBanner('browser'));
  window.addEventListener('online',()=>{if(_offlineVisible)checkOfflineRecoveryNow();});
  if(!_browserReportsOnline())showOfflineBanner('browser');
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',initOfflineMonitor,{once:true});
else initOfflineMonitor();
// Redirect to login when the server responds with 401 (auth session expired).
// Handles iOS PWA standalone mode and keeps subpath mounts like /hermes/ from
// escaping to the personal site root /login.
function _redirectIfUnauth(res){if(res&&res.status===401){window.location.href='login?next='+encodeURIComponent(window.location.pathname+window.location.search);return true;}return false;}
function _getSessionQueue(sid, create=false){
  if(!sid) return [];
  if(!SESSION_QUEUES[sid]&&create) SESSION_QUEUES[sid]=[];
  return SESSION_QUEUES[sid]||[];
}
function queueSessionMessage(sid, payload){
  if(!sid||!payload) return 0;
  const q=_getSessionQueue(sid,true);
  // Stamp created_at so the restore path can detect stale entries (agent already responded)
  const entry={...payload, _queued_at: Date.now()};
  q.push(entry);
  // Persist to sessionStorage so the queue survives page refresh
  try{ sessionStorage.setItem('hermes-queue-'+sid, JSON.stringify(q)); }catch(_){}
  return q.length;
}
function shiftQueuedSessionMessage(sid){
  const q=_getSessionQueue(sid,false);
  if(!q.length) return null;
  const next=q.shift();
  if(!q.length){
    delete SESSION_QUEUES[sid];
    try{ sessionStorage.removeItem('hermes-queue-'+sid); }catch(_){}
  } else {
    try{ sessionStorage.setItem('hermes-queue-'+sid, JSON.stringify(q)); }catch(_){}
  }
  return next;
}
function getQueuedSessionCount(sid){
  return _getSessionQueue(sid,false).length;
}
function _compressionSessionLock(){
  return window._compressionLockSid||null;
}
function _setCompressionSessionLock(sid){
  window._compressionLockSid=sid||null;
}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function _matchBacktickFenceLine(line){
  const m=String(line||'').match(/^[ ]{0,3}(`{3,})([^`]*)$/);
  if(!m) return null;
  return {fence:m[1],len:m[1].length,info:(m[2]||'').trim()};
}
function _isBacktickFenceClose(line,minLen){
  const m=String(line||'').match(/^[ ]{0,3}(`{3,})[ \t]*$/);
  return !!(m&&m[1].length>=minLen);
}
/**
 * Render fenced code blocks inside user messages.
 * Extracts ```…``` fences, replaces them with placeholders,
 * escapes remaining text as plain HTML, then restores code blocks
 * with the same <pre><code> pipeline used by renderMd().
 * All non-fenced text stays escaped (no bold/italic/link interpretation).
 */

function _stripWorkspaceDisplayPrefix(text){
  // v1 sentinel format `[Workspace::v1: <escaped path>]\n` injected since #1918.
  // Legacy format `[Workspace: <path>]\n` may still be present in transcripts
  // saved before the v1 migration; fall through to the legacy regex when the
  // v1 strip didn't match. Mirrors the Python `include_legacy=True` branch in
  // api/streaming.py:_strip_workspace_prefix(). Per Opus advisor on stage-322.
  const value = String(text||'');
  const stripped = value.replace(/^\s*\[Workspace::v1:\s*(?:\\.|[^\]\\])+\]\s*/,'');
  if(stripped !== value) return stripped.trim();
  return value.replace(/^\s*\[Workspace:[^\]]+\]\s*/,'').trim();
}
function _renderUserFencedBlocks(text){
  const stash=[];
  const mathStash=[];
  const stashMath=(type,src)=>{mathStash.push({type,src});return '\x00UM'+(mathStash.length-1)+'\x00';};
  const restoreMath=html=>String(html||'').replace(/\x00UM(\d+)\x00/g,(_,i)=>{
    const item=mathStash[+i];
    if(!item) return '';
    if(item.type==='display') return `<div class="katex-block" data-katex="display">${esc(item.src)}</div>`;
    return `<span class="katex-inline" data-katex="inline">${esc(item.src)}</span>`;
  });
  let s=String(text||'');
  // Extract fenced code blocks FIRST so math regexes never run inside fenced
  // content. If math were stashed first, a user-typed code block containing
  // \[..\] / \(..\) / $$..$$ would be rendered as a KaTeX block inside
  // <pre><code> instead of as literal source. Mirrors renderMd()'s ordering.
  // CommonMark §4.5 line-anchored fence: the closing run must use at least
  // as many backticks as the opener, so inner triple-backtick fences remain content.
  s=s.replace(/(^|\n)[ ]{0,3}(`{3,})([^\n`]*)\n(?:([\s\S]*?)\n)?[ ]{0,3}\2`*[ \t]*(?=\n|$)/g,(_,lead,_fence,info,code)=>{
    const langInfo=(info||'').trim();
    const langMatch=langInfo.match(/^(\w[\w+-]*)$/);
    let lang=langMatch?(langMatch[1]||'').trim().toLowerCase():'';
    code=code||'';
    // Remove one trailing newline if present (the fence consumes its own)
    if(code.endsWith('\n')) code=code.slice(0,-1);
    const h=lang?`<div class="pre-header">${esc(lang)}</div>`:'';
    const langAttr=lang?` class="language-${esc(lang)}"`:'';
    if(lang==='diff'||lang==='patch'){
      const colored=esc(code).split('\n').map(line=>{
        if(line.startsWith('@@')) return `<span class="diff-line diff-hunk">${line}</span>`;
        if(line.startsWith('+')) return `<span class="diff-line diff-plus">${line}</span>`;
        if(line.startsWith('-')) return `<span class="diff-line diff-minus">${line}</span>`;
        return `<span class="diff-line">${line}</span>`;
      }).join('\n');
      stash.push(`${h}<pre class="diff-block"><code${langAttr}>${colored}</code></pre>`);
    } else {
      stash.push(`${h}<pre><code${langAttr}>${esc(code)}</code></pre>`);
    }
    return lead+'\x00UF'+(stash.length-1)+'\x00';
  });
  // Now stash math from the OUTSIDE-of-fence text. Display delimiters must
  // run before inline so $$..$$ isn't mis-parsed as $..$..$..$.
  s=s.replace(/\$\$([\s\S]+?)\$\$/g,(_,m)=>stashMath('display',m));
  s=s.replace(/\\\[([\s\S]+?)\\\]/g,(_,m)=>stashMath('display',m));
  s=s.replace(/\$([^\s$\n][^$\n]*?[^\s$\n]|\S)\$/g,(_,m)=>stashMath('inline',m));
  s=s.replace(/\\\((.+?)\\\)/g,(_,m)=>stashMath('inline',m));
  // Escape remaining plain text and convert newlines to <br>
  s=esc(s).replace(/\n/g,'<br>');
  // Restore stashed code blocks, then math placeholders as KaTeX targets.
  s=s.replace(/\x00UF(\d+)\x00/g,(_,i)=>stash[+i]);
  s=restoreMath(s);
  return s;
}
function _statusCardHtml(card){
  card=card||{};
  const rows=Array.isArray(card.rows)?card.rows:[];
  const sessionId=String(card.sessionId||'');
  const shortSessionId=sessionId.length>22?`${sessionId.slice(0,10)}…${sessionId.slice(-8)}`:sessionId;
  const copyIcon=(typeof li==='function')?li('copy',13):'Copy';
  const copyBtn=sessionId
    ? `<button class="status-card-session-copy" type="button" data-copy-status-session="${esc(card.sessionId||'')}" title="${esc(t('copy'))}" onclick="copyStatusSessionId(this);event.stopPropagation()"><span>${esc(shortSessionId)}</span>${copyIcon}</button>`
    : '';
  const rowHtml=rows.map(row=>`
    <div class="status-card-row">
      <span class="status-card-label">${esc(row.label||'')}</span>
      <span class="status-card-value">${esc(row.value||'')}</span>
    </div>`).join('');
  return `<div class="status-card" data-status-card="1">
    <div class="status-card-head">
      <div class="status-card-title-wrap">
        <div class="status-card-title">${esc(card.title||t('status_heading'))}</div>
        <div class="status-card-subtitle">${esc(card.subtitle||'')}</div>
      </div>
      ${copyBtn}
    </div>
    <div class="status-card-grid">${rowHtml}</div>
  </div>`;
}

const MESSAGE_RENDER_WINDOW_DEFAULT=50;
let _messageRenderWindowSid=null;
let _messageRenderWindowSize=MESSAGE_RENDER_WINDOW_DEFAULT;
// Cached visWithIdx array — invalidated when S.messages.length changes.
let _visWithIdxCache=null;
let _visWithIdxCacheLen=0;
let _visWithIdxCacheSrc=null;  // S.messages reference — detects wholesale replacement with same length
function clearVisibleMessageRowCache(){
  _visWithIdxCache=null;
  _visWithIdxCacheLen=0;
  _visWithIdxCacheSrc=null;
}
function _resetMessageRenderWindow(sid){
  _messageRenderWindowSid=sid||null;
  _messageRenderWindowSize=MESSAGE_RENDER_WINDOW_DEFAULT;
  _clearRenderCache();
  clearVisibleMessageRowCache();
}

// ── renderMd / _renderUserFencedBlocks cache ──────────────────────────────
// Long sessions re-render the same messages on every renderMessages() call.
// Cache the rendered HTML so unchanged messages skip the expensive regex
// pipeline entirely.  ~95% of messages are identical between renders.
const _renderCache = new Map();
const _renderCacheMax = 300;
function _clearRenderCache(){ _renderCache.clear(); }
function _renderCacheKey(text, isUser){
  const p = isUser ? 'u' : 'a';
  // Short content: use the full string as key (cheap Map lookup).
  // Long content: length + prefix + suffix is good enough — collisions on
  // 20-char prefix+suffix are vanishingly rare for chat messages.
  if(text.length <= 500) return p + ':' + text;
  return p + ':' + text.length + ':' + text.slice(0,20) + ':' + text.slice(-20);
}
function _getCachedRender(text, isUser){
  const key = _renderCacheKey(text, isUser);
  const hit = _renderCache.get(key);
  if(hit !== undefined) return hit;
  const rendered = isUser
    ? _renderUserFencedBlocks(text)
    : renderMd(_stripXmlToolCallsDisplay(String(text)));
  if(_renderCache.size > _renderCacheMax) _renderCache.clear();
  _renderCache.set(key, rendered);
  return rendered;
}
function _currentMessageRenderWindowSize(){
  return Math.max(
    MESSAGE_RENDER_WINDOW_DEFAULT,
    Number(_messageRenderWindowSize)||MESSAGE_RENDER_WINDOW_DEFAULT
  );
}
function _messageRenderableMessageCount(){
  let count=0;
  for(const m of (S.messages||[])){
    if(!m||!m.role||m.role==='tool') continue;
    if(_isContextCompactionMessage(m)||_isPreservedCompressionTaskListMessage(m)) continue;
    if(_isRecoveryControlMessage(m)) continue;
    const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
    const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
    if(msgContent(m)||m._statusCard||m.attachments?.length||(m.role==='assistant'&&(hasTc||hasTu||_messageHasReasoningPayload(m)||_assistantMessageHasVisibleContent(m)))) count++;
  }
  return count;
}
function _messageHiddenBeforeCount(){
  return Math.max(0,_messageRenderableMessageCount()-_currentMessageRenderWindowSize());
}
function _isSessionEndlessScrollEnabled(){
  return window._sessionEndlessScrollEnabled===true;
}
function _wireMessageWindowLoadEarlierButton(){
  const indicator=$('loadOlderIndicator');
  if(!indicator) return;
  indicator.onclick=()=>{
    if(_messageHiddenBeforeCount()>0) _showEarlierRenderedMessages();
    else if(typeof _loadOlderMessages==='function') _loadOlderMessages();
  };
}
function _showEarlierRenderedMessages(){
  const container=$('messages');
  const prevScrollH=container?container.scrollHeight:0;
  const prevScrollTop=container?container.scrollTop:0;
  _messageRenderWindowSize=_currentMessageRenderWindowSize()+MESSAGE_RENDER_WINDOW_DEFAULT;
  renderMessages();
  if(container){
    const newScrollH=container.scrollHeight;
    container.scrollTop=prevScrollTop+(newScrollH-prevScrollH);
  }
  _scrollPinned=false;
}
function _isSessionJumpButtonsEnabled(){
  return window._sessionJumpButtonsEnabled===true;
}
function _applySessionNavigationPrefs(){
  const container=$('messages');
  if(container) container.classList.toggle('session-nav-enabled',_isSessionJumpButtonsEnabled());
  _updateSessionStartJumpButton();
}
function _updateSessionStartJumpButton(){
  const btn=$('jumpToSessionStartBtn');
  const container=$('messages');
  if(!btn||!container) return;
  if(!_isSessionJumpButtonsEnabled()){
    btn.style.display='none';
    return;
  }
  const hasSession=!!(S&&S.session&&S.messages&&S.messages.length);
  const awayFromStart=container.scrollTop>Math.max(240,container.clientHeight*0.35);
  const hasScrollableHistory=container.scrollHeight>container.clientHeight+Math.max(240,container.clientHeight*0.35);
  const canRevealStart=hasScrollableHistory||_messageHiddenBeforeCount()>0||!!(typeof _messagesTruncated!=='undefined'&&_messagesTruncated);
  btn.style.display=(hasSession&&canRevealStart&&awayFromStart)?'flex':'none';
}
async function jumpToSessionStart(){
  const container=$('messages');
  if(!container||!S.session) return;
  _scrollPinned=false;
  _messageUserUnpinned=true;
  _programmaticScroll=true;
  try{
    // During active streaming, skip full message load — API response won't
    // include live messages from the current turn, and replacing S.messages
    // would lose user/assistant/tool messages.
    if(!(S.busy||S.activeStreamId)){
      if(typeof _ensureAllMessagesLoaded==='function') await _ensureAllMessagesLoaded();
    }
    _messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(),_messageRenderableMessageCount());
    // During streaming, skip renderMessages — it rebuilds the DOM but tool card
    // insertion is blocked by !S.busy, losing Activity until "done" fires.
    if(!(S.busy||S.activeStreamId)){
      renderMessages({ preserveScroll:true });
    }
    requestAnimationFrame(()=>{
      container.scrollTop=0;
      _updateSessionStartJumpButton();
      requestAnimationFrame(()=>{ _programmaticScroll=false; });
    });
  }catch(e){
    console.warn('jumpToSessionStart failed:',e);
    _programmaticScroll=false;
  }
}

function _userMessageDomId(rawIdx){
  return `msg-user-${rawIdx}`;
}

function _questionJumpButtonHtml(questionRawIdx){
  if(typeof questionRawIdx!=='number'||questionRawIdx<0) return '';
  const label=t('jump_to_question')||'Question';
  const title=t('jump_to_question_label')||'Jump to the question for this response';
  return `<button class="msg-question-jump-btn" type="button" title="${esc(title)}" aria-label="${esc(title)}" onclick="jumpToTurnQuestion(${questionRawIdx})"><span aria-hidden="true">↑</span><span>${esc(label)}</span></button>`;
}

function _highlightQuestionRow(row){
  if(!row) return;
  row.classList.remove('msg-question-highlight');
  void row.offsetWidth;
  row.classList.add('msg-question-highlight');
  window.setTimeout(()=>row.classList.remove('msg-question-highlight'),1800);
}

async function jumpToTurnQuestion(questionRawIdx){
  const container=$('messages');
  if(!container||typeof questionRawIdx!=='number'||questionRawIdx<0) return;
  const scrollToTarget=()=>{
    const row=document.getElementById(_userMessageDomId(questionRawIdx));
    if(!row) return false;
    row.scrollIntoView({block:'center',behavior:'smooth'});
    _highlightQuestionRow(row);
    return true;
  };
  if(scrollToTarget()) return;
  if(_messageHiddenBeforeCount()>0){
    _messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(),_messageRenderableMessageCount());
    renderMessages({ preserveScroll:true });
    requestAnimationFrame(scrollToTarget);
  }
}

const DASHBOARD_STATUS_TTL_MS=60000;
let _dashboardStatusCache=null;
let _dashboardStatusFetchedAt=0;

function _dashboardIsBrowserLoopback(){
  const host=(window.location.hostname||'').replace(/^\[|\]$/g,'').toLowerCase();
  return host==='127.0.0.1'||host==='localhost'||host==='::1';
}
function _dashboardBrowserUrl(status){
  if(!status||!status.running) return '';
  if(status.browser_url||status.url){
    try{return new URL(status.browser_url||status.url).toString().replace(/\/$/,'');}
    catch(_){}
  }
  if(!status.port) return '';
  let source;
  try{source=new URL('http://127.0.0.1:'+status.port);}
  catch(_){return '';}
  const browserHost=window.location.hostname||source.hostname;
  const displayHost=browserHost.includes(':')&&!browserHost.startsWith('[')?'['+browserHost+']':browserHost;
  return source.protocol+'//'+displayHost+':'+status.port;
}
function _applyDashboardStatus(status){
  const running=!!(status&&status.running);
  const url=running?_dashboardBrowserUrl(status):'';
  const warning=running&&!_dashboardIsBrowserLoopback()?t('dashboard_loopback_warning'):'';
  document.querySelectorAll('[data-dashboard-link]').forEach(btn=>{
    btn.classList.toggle('dashboard-link-visible',running);
    btn.style.display=running?'':'none';
    btn.dataset.dashboardUrl=url;
    const tipText=warning||t('tab_dashboard');
    if(btn.hasAttribute('data-tooltip')){
      // Sync the custom CSS tooltip and explicitly clear the native title so
      // the slow ~1.5s native browser tooltip does not co-fire alongside the
      // fast custom tooltip (#1775).
      btn.setAttribute('data-tooltip',tipText);
      if(btn.hasAttribute('title')) btn.removeAttribute('title');
    } else {
      btn.title=tipText;
    }
    btn.setAttribute('aria-label',tipText);
  });
}
async function refreshDashboardStatus(force=false){
  const now=Date.now();
  if(!force&&_dashboardStatusCache&&(now-_dashboardStatusFetchedAt)<DASHBOARD_STATUS_TTL_MS){
    _applyDashboardStatus(_dashboardStatusCache);
    return _dashboardStatusCache;
  }
  try{
    const status=await api('/api/dashboard/status',{timeoutToast:false});
    _dashboardStatusCache=status||{running:false};
  }catch(_){
    _dashboardStatusCache={running:false};
  }
  _dashboardStatusFetchedAt=Date.now();
  _applyDashboardStatus(_dashboardStatusCache);
  return _dashboardStatusCache;
}
async function loadDashboardSettings(){
  const modeEl=$('settingsDashboardMode');
  const urlEl=$('settingsDashboardUrl');
  if(!modeEl&&!urlEl) return;
  try{
    const cfg=await api('/api/dashboard/config');
    if(modeEl) modeEl.value=cfg.enabled||'auto';
    if(urlEl) urlEl.value=cfg.url||'';
  }catch(_){/* leave defaults visible */}
}
async function saveDashboardSettings(){
  const modeEl=$('settingsDashboardMode');
  const urlEl=$('settingsDashboardUrl');
  const statusEl=$('settingsDashboardStatus');
  const payload={enabled:(modeEl&&modeEl.value)||'auto',url:(urlEl&&urlEl.value||'').trim()};
  try{
    const saved=await api('/api/dashboard/config',{method:'POST',body:JSON.stringify(payload)});
    if(modeEl) modeEl.value=saved.enabled||'auto';
    if(urlEl) urlEl.value=saved.url||'';
    if(statusEl) statusEl.textContent='Dashboard link settings saved.';
    await refreshDashboardStatus(true);
  }catch(err){
    if(statusEl) statusEl.textContent='Dashboard link settings failed to save.';
    else if(typeof showToast==='function') showToast('Dashboard link settings failed to save.');
  }
}
function openHermesDashboard(event){
  if(event){event.preventDefault();event.stopPropagation();}
  const btn=event&&event.currentTarget?event.currentTarget:document.querySelector('[data-dashboard-link]');
  const url=(btn&&btn.dataset&&btn.dataset.dashboardUrl)||_dashboardBrowserUrl(_dashboardStatusCache);
  if(!url) return false;
  window.open(url,'_blank','noopener,noreferrer');
  return false;
}
function _initDashboardLinkProbe(){
  loadDashboardSettings();
  refreshDashboardStatus(true);
  setInterval(refreshDashboardStatus,DASHBOARD_STATUS_TTL_MS);
}
if(document.readyState==='complete'){
  _initDashboardLinkProbe();
}else{
  document.addEventListener('DOMContentLoaded',_initDashboardLinkProbe,{once:true});
}

/* ── Image lightbox — click any .msg-media-img to enlarge ─────────────────── */
function _openImgLightbox(imgEl) {
  if(!imgEl || !imgEl.src) return;
  const src=imgEl.src, alt=imgEl.alt||'';
  // Find sibling images in the same message for prev/next navigation.
  // Walk up from the clicked image to find the message container, then
  // collect all .msg-media-img within it.
  // Composer attach-tray chips bypass sibling detection — each chip click
  // opens a single-image lightbox (no navigation between staged uploads).
  let allImages = [];
  let startIndex = 0;
  if(!imgEl.closest('.attach-tray')){
    let container = imgEl.closest('.msg-row, .assistant-turn-blocks, .assistant-turn, .user-turn');
    if(!container) container = imgEl.parentElement;
    if(container){
      const siblings = container.querySelectorAll('.msg-media-img');
      if(siblings.length>1){
        allImages = Array.from(siblings);
        startIndex = allImages.indexOf(imgEl);
        if(startIndex===-1) startIndex=0;
      }
    }
  }
  _openImgLightboxWithNav(src, alt, allImages, startIndex);
}
function _openImgLightboxWithNav(src, alt, images, index) {
  const lb = document.createElement('div');
  lb.className = 'img-lightbox';
  lb.setAttribute('role', 'dialog');
  lb.setAttribute('aria-label', alt || 'Image');
  const img = document.createElement('img');
  img.src = src;
  img.alt = alt || '';
  img.onclick = e => e.stopPropagation();
  const cls = document.createElement('button');
  cls.className = 'img-lightbox-close';
  cls.setAttribute('aria-label', 'Close');
  cls.textContent = '×';
  cls.onclick = () => _closeImgLightbox(lb);
  lb.appendChild(img);
  lb.appendChild(cls);
  // Prev/Next navigation — store index and images on lb so a single set of
  // handlers reads live values without closure churn on every nav.
  lb._navIndex = index;
  lb._navImages = (images && images.length>1) ? images : null;
  if(lb._navImages){
    const prevBtn = document.createElement('button');
    prevBtn.className = 'img-lightbox-nav img-lightbox-nav-prev';
    prevBtn.setAttribute('aria-label', 'Previous image');
    prevBtn.innerHTML = '‹';
    prevBtn.onclick = e => { e.stopPropagation(); _navigateLightbox(lb, -1); };
    lb.appendChild(prevBtn);
    const nextBtn = document.createElement('button');
    nextBtn.className = 'img-lightbox-nav img-lightbox-nav-next';
    nextBtn.setAttribute('aria-label', 'Next image');
    nextBtn.innerHTML = '›';
    nextBtn.onclick = e => { e.stopPropagation(); _navigateLightbox(lb, 1); };
    lb.appendChild(nextBtn);
    lb._counterEl = document.createElement('div');
    lb._counterEl.className = 'img-lightbox-counter';
    lb.appendChild(lb._counterEl);
    lb._counterEl.textContent = (index+1) + ' / ' + images.length;
  }
  lb.onclick = () => _closeImgLightbox(lb);
  document.body.appendChild(lb);
  // Single keyboard handler — reads lb._navX live, no remove/add churn.
  lb._keyHandler = e => {
    if(e.key==='Escape'){ _closeImgLightbox(lb); return; }
    if(lb._navImages){
      if(e.key==='ArrowLeft'){ e.preventDefault(); _navigateLightbox(lb, -1); }
      if(e.key==='ArrowRight'){ e.preventDefault(); _navigateLightbox(lb, 1); }
    }
  };
  document.addEventListener('keydown', lb._keyHandler);
}
function _navigateLightbox(lb, direction) {
  const images = lb._navImages;
  if(!images) return;
  const newIndex = lb._navIndex + direction;
  if(newIndex<0 || newIndex>=images.length) return;
  lb._navIndex = newIndex;
  const nextImg = images[newIndex];
  const lbImg = lb.querySelector('img');
  if(!lbImg) return;
  lbImg.src = nextImg.src;
  lbImg.alt = nextImg.alt || '';
  lb.setAttribute('aria-label', nextImg.alt || 'Image');
  // Update counter via stored reference — no DOM query.
  if(lb._counterEl) lb._counterEl.textContent = (newIndex+1) + ' / ' + images.length;
}
function _closeImgLightbox(lb) {
  if(!lb || !lb.parentNode) return;
  document.removeEventListener('keydown', lb._keyHandler);
  lb.style.animation = 'lb-in .12s ease reverse';
  setTimeout(() => lb.parentNode && lb.parentNode.removeChild(lb), 120);
}

document.addEventListener('click', e => {
  if(!e.target || !e.target.closest) return;
  const sessionLink=e.target.closest('a.session-link[href]');
  if(sessionLink){
    const href=sessionLink.getAttribute('href')||'';
    const m=href.match(/(?:^|\/)session\/([^?#]+)/i);
    if(m&&typeof loadSession==='function'){
      e.preventDefault();
      try{loadSession(decodeURIComponent(m[1]));}catch(_){loadSession(m[1]);}
    }
    return;
  }
  const workspaceLink=e.target.closest('a[href^="#workspace="]');
  if(workspaceLink){
    e.preventDefault();
    const href=workspaceLink.getAttribute('href')||'';
    try{
      const rel=decodeURIComponent(href.slice('#workspace='.length));
      if(rel && typeof openArtifactPath==='function') openArtifactPath(rel);
    }catch(_){}
    return;
  }
  // Message-attached images (already wired since v0.50.x).
  let img = e.target.closest('.msg-media-img');
  if(img){ _openImgLightbox(img); return; }
  // Composer attach-tray image thumbnails — click any pasted/dropped image
  // chip to lightbox-zoom it before sending. Excludes audio/video chips,
  // which keep their inline media controls. SVG thumbnails (.attach-thumb--svg)
  // are still images visually, so they qualify.
  img = e.target.closest('.attach-thumb');
  if(img && img.tagName === 'IMG'){
    _openImgLightbox(img);
    return;
  }
});

const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _PDF_EXTS=/\.pdf$/i;
const _HTML_EXTS=/\.(html?|htm)$/i;
const _ARCHIVE_EXTS=/\.(zip|tar|tar\.gz|tgz|tar\.bz2|tbz2|tar\.xz|txz)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm|oga)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
const _CSV_EXTS=/\.csv$/i;
const _EXCALIDRAW_EXTS=/\.excalidraw$/i;
// ── Media playback speed controls ─────────────────────────────────────────
const MEDIA_PLAYBACK_RATES=[0.5,0.75,1,1.25,1.5,2];
const MEDIA_PLAYBACK_STORAGE_KEY='hermes-media-playback-rate';
function _getStoredMediaPlaybackRate(){
  try{
    const raw=localStorage.getItem(MEDIA_PLAYBACK_STORAGE_KEY);
    const rate=Number(raw);
    return MEDIA_PLAYBACK_RATES.includes(rate)?rate:1;
  }catch(_){return 1;}
}
function _setStoredMediaPlaybackRate(rate){
  if(!MEDIA_PLAYBACK_RATES.includes(rate)) return;
  try{localStorage.setItem(MEDIA_PLAYBACK_STORAGE_KEY,String(rate));}catch(_){}
}
function _syncMediaSpeedButtons(editor, rate){
  if(!editor) return;
  editor.querySelectorAll('.media-speed-btn').forEach(b=>{
    const active=Number(b.dataset.rate)===rate;
    b.classList.toggle('active',active);
    b.setAttribute('aria-pressed',active?'true':'false');
  });
}
function _applyMediaPlaybackRate(media, rate=_getStoredMediaPlaybackRate()){
  if(!media) return;
  media.playbackRate=rate;
  _syncMediaSpeedButtons(media.closest('.msg-media-editor,.preview-media-wrap'),rate);
}
function _mediaKindForName(name=''){
  const clean=String(name||'').split('?')[0].toLowerCase();
  if(_AUDIO_EXTS.test(clean)) return 'audio';
  if(_VIDEO_EXTS.test(clean)) return 'video';
  if(_IMAGE_EXTS.test(clean)) return 'image';
  return '';
}
function _mediaSpeedControlsHtml(kind, label){
  const safeLabel=esc(label||kind||'media');
  const current=_getStoredMediaPlaybackRate();
  return `<div class="media-speed-controls" role="group" aria-label="Playback speed for ${safeLabel}">${MEDIA_PLAYBACK_RATES.map(rate=>`<button type="button" class="media-speed-btn${rate===current?' active':''}" data-rate="${rate}" aria-pressed="${rate===current?'true':'false'}">${rate}×</button>`).join('')}</div>`;
}
function _mediaPlayerHtml(kind, src, name, extra=''){
  const safeName=esc(name||'media');
  const safeSrc=esc(src);
  const tag=kind==='video'
    ? `<video class="msg-media-player msg-media-video" src="${safeSrc}" controls preload="metadata" playsinline title="${safeName}"></video>`
    : `<audio class="msg-media-player msg-media-audio" src="${safeSrc}" controls preload="metadata" title="${safeName}"></audio>`;
  return `<div class="msg-media-editor msg-media-editor--${kind}" data-media-kind="${kind}">${tag}<div class="msg-media-meta"><span class="msg-media-name">${safeName}</span>${extra}</div>${_mediaSpeedControlsHtml(kind,safeName)}</div>`;
}
function _renderAttachmentHtml(fname, url){
  const kind=_mediaKindForName(fname);
  if(kind==='image') return `<img class="msg-media-img" src="${esc(url)}" alt="${esc(fname)}" loading="lazy">`;
  if(kind==='audio'||kind==='video') return _mediaPlayerHtml(kind,url,fname);
  if(_HTML_EXTS.test(fname)){
    const inlineUrl=url+(String(url).includes('?')?'&':'?')+'inline=1';
    return `<a class="msg-file-badge msg-file-badge--html" href="${esc(inlineUrl)}" target="_blank" rel="noopener">${li('file-code',12)} ${esc(fname)}</a>`;
  }
  return `<div class="msg-file-badge">${li('paperclip',12)} ${esc(fname)}</div>`;
}
document.addEventListener('click', e => {
  const btn=e.target&&e.target.closest?e.target.closest('.media-speed-btn'):null;
  if(!btn) return;
  const editor=btn.closest('.msg-media-editor,.preview-media-wrap');
  if(!editor) return;
  const media=editor.querySelector('audio,video');
  if(!media) return;
  const rate=Number(btn.dataset.rate)||1;
  _setStoredMediaPlaybackRate(rate);
  _applyMediaPlaybackRate(media,rate);
});
document.addEventListener("loadedmetadata", e=>{
  if(e.target&&e.target.matches&&e.target.matches('.msg-media-player,audio,video')){
    _applyMediaPlaybackRate(e.target);
  }
},true);
function _initMediaPlaybackObserver(){
  if(!document.body||window._mediaPlaybackObserver) return;
  window._mediaPlaybackObserver=new MutationObserver(records=>{
    for(const rec of records){
      for(const node of rec.addedNodes||[]){
        if(!node||node.nodeType!==1) continue;
        const media=[];
        if(node.matches&&node.matches('audio,video')) media.push(node);
        if(node.querySelectorAll) media.push(...node.querySelectorAll('audio,video'));
        media.forEach(m=>_applyMediaPlaybackRate(m));
      }
    }
  });
  window._mediaPlaybackObserver.observe(document.body,{childList:true,subtree:true});
  document.querySelectorAll('audio,video').forEach(m=>_applyMediaPlaybackRate(m));
}
if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',_initMediaPlaybackObserver);
else _initMediaPlaybackObserver();
setTimeout(_initMediaPlaybackObserver,0);

// ── Ambient provider quota indicator (#1766) ────────────────────────────────
let _providerQuotaRefreshInFlight=false;

function _formatQuotaMoneyShort(value){
  const n=Number(value);
  if(!Number.isFinite(n)) return '';
  if(Math.abs(n)>=100) return '$'+n.toFixed(0);
  if(Math.abs(n)>=10) return '$'+n.toFixed(1);
  return '$'+n.toFixed(2);
}
function _formatQuotaPercentShort(value){
  const n=Number(value);
  if(!Number.isFinite(n)) return '';
  return Math.max(0,Math.min(100,n)).toFixed(0)+'%';
}
function _providerQuotaIndicatorText(status){
  if(!status||status.status!=='available') return null;
  const provider=status.display_name||status.provider||'Provider';
  const accountLimits=status.account_limits||null;
  if(accountLimits&&Array.isArray(accountLimits.windows)&&accountLimits.windows.length){
    const w=accountLimits.windows.find(x=>x&&Number.isFinite(Number(x.remaining_percent)))||accountLimits.windows[0];
    const remaining=_formatQuotaPercentShort(w&&w.remaining_percent);
    if(remaining) return {label:provider+' '+remaining, title:(status.message||'Provider usage loaded')+' — '+remaining+' remaining'};
  }
  const quota=status.quota||null;
  if(quota){
    const remaining=_formatQuotaMoneyShort(quota.limit_remaining);
    const used=_formatQuotaMoneyShort(quota.usage);
    const limit=_formatQuotaMoneyShort(quota.limit);
    if(remaining){
      const parts=[];
      if(used) parts.push('used '+used);
      if(limit) parts.push('limit '+limit);
      return {label:provider+' '+remaining, title:(status.message||'Provider quota loaded')+(parts.length?' — '+parts.join(' · '):'')};
    }
  }
  return null;
}
function renderProviderQuotaIndicator(status){
  const chip=$('providerQuotaChip');
  const label=$('providerQuotaChipLabel');
  if(!chip||!label) return;
  // Hide entirely when the user has disabled the ambient quota chip in Settings.
  // Default is off (window._showQuotaChip defaults to false in boot.js) so users
  // never see the chip unless they opt in.
  if(window._showQuotaChip!==true){
    chip.hidden=true;
    label.textContent='';
    chip.removeAttribute('title');
    return;
  }
  const text=_providerQuotaIndicatorText(status);
  if(!text||status.status!=='available'||(!status.quota&&!status.account_limits)){
    chip.hidden=true;
    label.textContent='';
    chip.removeAttribute('title');
    return;
  }
  label.textContent=text.label;
  chip.title=text.title;
  chip.hidden=false;
}
async function refreshProviderQuotaIndicator(){
  // Short-circuit before the fetch when the chip is disabled — no point asking
  // the server for quota data the UI will throw away.
  if(window._showQuotaChip!==true){
    const chip=$('providerQuotaChip');
    if(chip){chip.hidden=true;chip.removeAttribute('title');}
    return;
  }
  if(_providerQuotaRefreshInFlight) return;
  _providerQuotaRefreshInFlight=true;
  try{
    const status=await api('/api/provider/quota');
    renderProviderQuotaIndicator(status);
  }catch(_e){
    renderProviderQuotaIndicator(null);
  }finally{
    _providerQuotaRefreshInFlight=false;
  }
}
window.addEventListener('visibilitychange',()=>{
  if(document.visibilityState==='visible'&&typeof refreshProviderQuotaIndicator==='function') refreshProviderQuotaIndicator();
});

// Dynamic model labels -- populated by populateModelDropdown(), fallback to static map
let _dynamicModelLabels={};
window._configuredModelBadges=window._configuredModelBadges||{};
const MODEL_STATE_KEY='hermes-webui-model-state';
const PENDING_SESSION_MODEL_PREFIX='hermes-webui-pending-session-model:';
const PENDING_SESSION_MODEL_MAX_AGE_MS=10*60*1000;

// ── Smart model resolver ────────────────────────────────────────────────────
// Finds the best matching option value in a <select> for a given model ID.
// Handles mismatches like 'claude-sonnet-4-6' vs 'anthropic/claude-sonnet-4.6'.
// When a preferred provider is supplied, duplicate normalized IDs prefer that
// provider's option so Settings/profile rehydration doesn't snap back to the
// first colliding entry.
function _getOptionProviderId(opt){
  if(!opt) return '';
  if(opt.dataset && opt.dataset.provider) return opt.dataset.provider;
  const group=opt.parentElement;
  if(group && group.tagName==='OPTGROUP' && group.dataset && group.dataset.provider){
    return group.dataset.provider;
  }
  const value=String(opt.value||'');
  if(value.startsWith('@') && value.includes(':')) return value.slice(1,value.lastIndexOf(':'));
  return '';
}
function _providerFromModelValue(modelId){
  const value=String(modelId||'').trim();
  if(value.startsWith('@')&&value.includes(':')) return value.slice(1,value.lastIndexOf(':'));
  return '';
}
function _providerSkipsModelMismatchWarning(providerId){
  const p=String(providerId||'').toLowerCase();
  return !p||p==='custom'||p.startsWith('custom:')||p==='openrouter';
}
function _providerDefersMissingModelFallback(providerId){
  const p=String(providerId||'').toLowerCase();
  // Named custom providers and OpenRouter can legitimately route vendor-prefixed
  // model IDs that are not present in the current static catalog. Do not
  // silently rewrite those sessions to the default just because the option has
  // not been hydrated yet (#2405).
  return p.startsWith('custom:')||p==='openrouter';
}
function _modelStateForSelect(sel, modelId){
  const value=String(modelId||'').trim();
  if(!value) return {model:'',model_provider:null};
  const explicitProvider=_providerFromModelValue(value);
  if(explicitProvider) return {model:value,model_provider:explicitProvider};
  const opt=sel&&sel.selectedOptions&&sel.selectedOptions[0];
  const provider=String(_getOptionProviderId(opt)||'').trim();
  return {model:value,model_provider:(provider&&provider!=='default')?provider:null};
}
function _captureModelDropdownSelection(sel){
  if(!sel||!sel.value) return null;
  try{
    const state=_modelStateForSelect(sel,sel.value);
    if(state&&state.model) return state;
  }catch(_){}
  return {model:String(sel.value||''),model_provider:null};
}
function _modelProviderForSend(modelId){
  const sessionProvider=(S&&S.session&&S.session.model_provider)||null;
  if(sessionProvider) return sessionProvider;
  const model=String(modelId||'').trim();
  if(!model) return null;
  const explicitProvider=typeof _providerFromModelValue==='function'
    ? _providerFromModelValue(model)
    : '';
  if(explicitProvider) return explicitProvider;
  const sel=typeof $==='function' ? $('modelSelect') : null;
  if(sel&&String(sel.value||'').trim()===model&&typeof _modelStateForSelect==='function'){
    try{
      const dropdownState=_modelStateForSelect(sel,sel.value);
      if(dropdownState&&String(dropdownState.model||'').trim()===model){
        return dropdownState.model_provider||null;
      }
    }catch(_){}
  }
  if(typeof _readPersistedModelState==='function'){
    try{
      const persisted=_readPersistedModelState();
      if(persisted&&String(persisted.model||'').trim()===model){
        return persisted.model_provider||null;
      }
    }catch(_){}
  }
  return null;
}
function _reconcileModelDropdownSelection(sel,data,previousState,opts){
  if(!sel) return null;
  const activeSession=(typeof S!=='undefined'&&S&&S.session)?S.session:null;
  // Fresh boot is the only path where the profile/server default intentionally
  // beats a browser-persisted or static fallback value. Every other model-list
  // rebuild should preserve the loaded session model or the user's current
  // in-page selection when it still exists in the refreshed catalog.
  const shouldApplyBootDefault=!!(opts&&opts.preferProfileDefaultOnFreshBoot);
  if(shouldApplyBootDefault && data&&data.default_model && !(activeSession&&activeSession.model)){
    return _applyModelToDropdown(data.default_model,sel,data.active_provider||null);
  }
  if(activeSession&&activeSession.model){
    return _applyModelToDropdown(activeSession.model,sel,activeSession.model_provider||null);
  }
  if(previousState&&previousState.model){
    return _applyModelToDropdown(previousState.model,sel,previousState.model_provider||null);
  }
  return null;
}
function _providerQualifiedModelValueForSelect(sel, modelId){
  return _modelStateForSelect(sel,modelId).model;
}
function _readPersistedModelState(){
  try{
    const raw=localStorage.getItem(MODEL_STATE_KEY);
    if(raw){
      const parsed=JSON.parse(raw);
      if(parsed&&parsed.model){
        return {
          model:String(parsed.model||''),
          model_provider:parsed.model_provider?String(parsed.model_provider):(_providerFromModelValue(parsed.model)||null),
        };
      }
    }
  }catch(_){}
  const legacy=localStorage.getItem('hermes-webui-model');
  if(!legacy) return null;
  return {model:legacy,model_provider:_providerFromModelValue(legacy)||null};
}
function _writePersistedModelState(model, modelProvider){
  const value=String(model||'').trim();
  const provider=modelProvider?String(modelProvider).trim():(_providerFromModelValue(value)||null);
  if(!value){
    localStorage.removeItem('hermes-webui-model');
    localStorage.removeItem(MODEL_STATE_KEY);
    return;
  }
  localStorage.setItem('hermes-webui-model', value);
  try{
    localStorage.setItem(MODEL_STATE_KEY, JSON.stringify({model:value,model_provider:provider||null}));
  }catch(_){}
}
function _clearPersistedModelState(){
  localStorage.removeItem('hermes-webui-model');
  localStorage.removeItem(MODEL_STATE_KEY);
}
function _pendingSessionModelKey(sessionId){
  return PENDING_SESSION_MODEL_PREFIX+String(sessionId||'');
}
function _rememberPendingSessionModel(sessionId, model, modelProvider){
  const sid=String(sessionId||'').trim();
  const value=String(model||'').trim();
  if(!sid||!value) return;
  const provider=modelProvider?String(modelProvider).trim():(_providerFromModelValue(value)||null);
  try{
    sessionStorage.setItem(_pendingSessionModelKey(sid), JSON.stringify({
      model:value,
      model_provider:provider||null,
      saved_at:Date.now(),
    }));
  }catch(_){}
}
function _readPendingSessionModel(sessionId){
  const sid=String(sessionId||'').trim();
  if(!sid) return null;
  try{
    const raw=sessionStorage.getItem(_pendingSessionModelKey(sid));
    if(!raw) return null;
    const parsed=JSON.parse(raw);
    const model=String(parsed&&parsed.model||'').trim();
    if(!model){
      sessionStorage.removeItem(_pendingSessionModelKey(sid));
      return null;
    }
    const savedAt=Number(parsed.saved_at||0);
    if(savedAt&&Date.now()-savedAt>PENDING_SESSION_MODEL_MAX_AGE_MS){
      sessionStorage.removeItem(_pendingSessionModelKey(sid));
      return null;
    }
    return {
      model,
      model_provider:parsed&&parsed.model_provider?String(parsed.model_provider):(_providerFromModelValue(model)||null),
    };
  }catch(_){
    try{sessionStorage.removeItem(_pendingSessionModelKey(sid));}catch(__){}
    return null;
  }
}
function _clearPendingSessionModel(sessionId){
  const sid=String(sessionId||'').trim();
  if(!sid) return;
  try{sessionStorage.removeItem(_pendingSessionModelKey(sid));}catch(_){}
}
function _applyPendingSessionModelForSession(sessionId){
  if(!S.session||S.session.session_id!==sessionId) return false;
  const pending=_readPendingSessionModel(sessionId);
  if(!pending) return false;
  const sameModel=String(S.session.model||'')===pending.model;
  const sameProvider=String(S.session.model_provider||'')===String(pending.model_provider||'');
  if(sameModel&&sameProvider){
    _clearPendingSessionModel(sessionId);
    return false;
  }
  S.session.model=pending.model;
  S.session.model_provider=pending.model_provider||null;
  const retry=_persistSessionModelCorrection(pending.model,pending.model_provider||null,{propagateErrors:true});
  if(retry&&typeof retry.then==='function'){
    retry.then(()=>_clearPendingSessionModel(sessionId)).catch(()=>{});
  }
  return true;
}
function _findModelInDropdown(modelId, sel, preferredProviderId){
  if(!modelId||!sel) return null;
  const options=Array.from(sel.options);
  const opts=options.map(o=>o.value);
  // 0. Exact match — highest priority when it doesn't conflict with a
  // cross-provider preference (#3360, guarded for #1228/#1313).
  // When all models share the same provider (e.g. a custom proxy),
  // normalization can collapse distinct multi-slash IDs to the same key
  // and options.find() returns whichever appears first in the DOM instead
  // of the exact value.  But when the exact option belongs to a *different*
  // provider than the preferred one, we must fall through to the provider-
  // aware match so rehydration doesn't snap to the wrong provider row.
  if(opts.includes(modelId)){
    const exactOpt=options.find(o=>o.value===modelId);
    const exactProv=exactOpt?_getOptionProviderId(exactOpt).toLowerCase():'';
    const pref=String(preferredProviderId||'').toLowerCase();
    if(!pref || !exactProv || exactProv===pref) return modelId;
  }
  // 1. Normalize: lowercase, strip namespace prefix, replace hyphens→dots.
  // Also strip @provider: prefix from deduplicated model IDs (#1228, #1313).
  const norm=s=>s.toLowerCase().replace(/^[^/]+\//,'').replace(/^@([^:]+:)+/,'').replace(/-/g,'.');
  const target=norm(modelId);
  let explicitProvider='';
  const rawModel=String(modelId||'');
  if(rawModel.startsWith('@')&&rawModel.includes(':')){
    explicitProvider=rawModel.slice(1,rawModel.lastIndexOf(':'));
  }
  const preferred=String(preferredProviderId||explicitProvider||'').toLowerCase();
  if(preferred){
    const providerMatch=options.find(o=>norm(o.value)===target && _getOptionProviderId(o).toLowerCase()===preferred);
    if(providerMatch) return providerMatch.value;
  }
  // 2. Normalized match
  const exact=opts.find(o=>norm(o)===target);
  if(exact) return exact;
  // If the request is provider-qualified (either explicit @provider:model or
  // a slash-qualified vendor/model id), do NOT fuzzy-match a sibling model
  // once exact/provider-aware lookup failed. Returning null lets the caller
  // preserve the raw typed value instead of snapping to the closest catalog
  // entry. This keeps uncatalogued models routable instead of silently turning
  // them into a nearby curated sibling.
  if(rawModel.startsWith('@')||rawModel.includes('/')) return null;
  // 3. Prefix/substring: require the candidate to start with the FULL normalized target
  // (not a truncated base). This avoids false matches like gpt.5.5 → gpt.5.4.mini (#1188).
  // Only fall back to the shorter base form if target itself is very short (a bare root
  // like "gpt" or "claude") where stripping would be a no-op anyway.
  const base=target.replace(/\.\d+$/,'');  // strip trailing version number
  const useBase=base.length<=4||base===target; // bare root — stripping changed nothing meaningful
  const prefixTarget=useBase?base:target;
  // When the typed target is a COMPLETE versioned name (ends in a digit, e.g.
  // "mimo-v2.5" → norm "mimo.v2.5"), a prefix hit on a longer option is only
  // legitimate if the extra text continues the VERSION ("." + digit, e.g.
  // mimo.v2 → mimo.v2.5...). If the extra text is a variant/tier suffix
  // ("." + non-digit, e.g. mimo.v2.5.pro from "mimo-v2.5-pro"), the user asked
  // for the base model that simply isn't in the catalog — do NOT silently snap
  // them to the -pro/-flash tier (and a different price tier). Let resolution
  // fall through to null so the caller reports no-match instead. (#3368)
  const targetEndsInVersion=/\d$/.test(target);
  const partial=opts.find(o=>{
    const no=norm(o);
    if(!no.startsWith(prefixTarget)) return false;
    if(targetEndsInVersion && no!==target){
      const rest=no.slice(target.length);
      // reject "." + non-digit (variant/tier suffix); allow "" or "." + digit (version continuation)
      if(rest && !/^\.\d/.test(rest)) return false;
    }
    return true;
  });
  return partial||null;
}

// Set the model picker to the best match for modelId.
// Returns the resolved value that was actually set, or null if nothing matched.
function _refreshOpenModelDropdown(){
  const dd=$('composerModelDropdown');
  if(dd&&dd.classList&&dd.classList.contains('open')&&typeof renderModelDropdown==='function'){
    renderModelDropdown();
    if(typeof _positionModelDropdown==='function') _positionModelDropdown();
  }
}
function _applyModelToDropdown(modelId, sel, preferredProviderId){
  if(!modelId||!sel) return null;
  const resolved=_findModelInDropdown(modelId,sel,preferredProviderId);
  if(resolved){
    sel.value=resolved;
    if(sel.id==='modelSelect'){
      if(typeof syncModelChip==='function') syncModelChip();
      _refreshOpenModelDropdown();
    }
    return resolved;
  }
  return null;
}
function _ensureModelOptionInDropdown(modelId, sel, preferredProviderId){
  if(!modelId||!sel) return null;
  const applied=_applyModelToDropdown(modelId,sel,preferredProviderId);
  if(applied) return applied;
  const value=modelId;
  const opt=document.createElement('option');
  opt.value=modelId;
  opt.textContent=typeof getModelLabel==='function'?getModelLabel(modelId):modelId;
  opt.dataset.custom='1';
  const badge=(window._configuredModelBadges||{})[value];
  if(badge&&badge.provider) opt.dataset.provider=badge.provider;
  const provider=preferredProviderId||(badge&&badge.provider)||_providerFromModelValue(modelId)||'';
  if(provider) opt.dataset.provider=provider;
  sel.appendChild(opt);
  sel.value=modelId;
  if(sel.id==='modelSelect'){
    if(typeof syncModelChip==='function') syncModelChip();
    _refreshOpenModelDropdown();
  }
  return modelId;
}
function _modelStateFromAppliedDropdown(sel, modelValue){
  const state=(typeof _modelStateForSelect==='function')
    ? _modelStateForSelect(sel,modelValue)
    : {model:modelValue,model_provider:null};
  return {model:state.model||modelValue,model_provider:state.model_provider||null};
}
function _persistSessionModelCorrection(model, provider, opts){
  if(!S.session) return;
  const request=fetch(new URL('api/session/update',document.baseURI||location.href).href,{
    method:'POST',credentials:'include',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({session_id:S.session.id||S.session.session_id,model:model,model_provider:provider||null})
  });
  return opts&&opts.propagateErrors ? request : request.catch(()=>{});
}
function _applySessionModelFallback(sel){
  if(!sel) return null;
  const configuredDefault=String(window._defaultModel||'').trim();
  if(configuredDefault){
    const appliedDefault=_applyModelToDropdown(configuredDefault,sel,window._activeProvider||null);
    if(appliedDefault) return _modelStateFromAppliedDropdown(sel,appliedDefault);
  }
  const first=sel.querySelector('optgroup > option, option');
  if(first){
    sel.value=first.value;
    if(sel.id==='modelSelect'){
      if(typeof syncModelChip==='function') syncModelChip();
      _refreshOpenModelDropdown();
    }
    return _modelStateFromAppliedDropdown(sel,first.value);
  }
  return null;
}

async function populateModelDropdown(opts={}){
  const sel=$('modelSelect');
  if(!sel) return;
  try{
    const _modelsRes=await fetch(new URL('api/models',document.baseURI||location.href).href,{credentials:'include'});
    if(_redirectIfUnauth(_modelsRes)) return;
    const data=await _modelsRes.json();
    // Store active provider globally so the send path can warn on mismatch
    window._activeProvider=data.active_provider||null;
    // Store default model so newSession() can apply it (#872).
    // Per-page-load — not synced across browser tabs.
    window._defaultModel=data.default_model||null;
    window._configuredModelBadges=data.configured_model_badges||{};
    // Keep the g.extra_models label hydration path below in this function; tests
    // assert populateModelDropdown preserves that full-catalog label contract.
    window._modelEndpointErrors={};

    const _synthGroupsFromConfigured=()=>{
      const badgeMap=window._configuredModelBadges||{};
      const grouped=new Map();
      const addModel=(providerId,modelId)=>{
        const pid=String(providerId||'configured').trim()||'configured';
        const mid=String(modelId||'').trim();
        if(!mid) return;
        if(!grouped.has(pid)) grouped.set(pid,[]);
        const arr=grouped.get(pid);
        if(arr.some(m=>m.id===mid)) return;
        arr.push({id:mid,label:getModelLabel(mid)});
      };

      for(const [modelId,badge] of Object.entries(badgeMap)){
        const mid=String(modelId||'').trim();
        // Prefer canonical IDs only; skip derived aliases such as
        // @provider:model and provider/model to avoid noisy duplicates.
        if(!mid||mid.startsWith('@')||mid.includes('/')) continue;
        const provider=(badge&&badge.provider)||'configured';
        addModel(provider,mid);
      }

      if(grouped.size===0&&data&&data.default_model){
        addModel(data.active_provider||'configured',data.default_model);
      }

      const groups=[];
      for(const [providerId,models] of grouped.entries()){
        const display=(String(providerId).startsWith('custom:')
          ? String(providerId).slice('custom:'.length)
          : String(providerId))||'Configured';
        groups.push({provider:display,provider_id:providerId,models});
      }
      return groups;
    };

    const groups=(Array.isArray(data.groups)&&data.groups.length)
      ? data.groups
      : _synthGroupsFromConfigured();

    if(!groups.length) return; // no server groups and no configured fallback
    const previousSelection=_captureModelDropdownSelection(sel);
    // Clear existing options
    sel.innerHTML='';
    _dynamicModelLabels={};
    for(const g of groups){
      const og=document.createElement('optgroup');
      og.label=g.provider;
      if(g.provider_id) og.dataset.provider=g.provider_id;
      if(g.models_endpoint_error){
        const errorKey=g.provider_id||g.provider||'';
        og.dataset.modelsEndpointError=JSON.stringify(g.models_endpoint_error);
        if(errorKey) window._modelEndpointErrors[errorKey]=g.models_endpoint_error;
      }
      for(const m of (Array.isArray(g.models)?g.models:[])){
        const opt=document.createElement('option');
        opt.value=m.id;
        opt.textContent=m.label;
        og.appendChild(opt);
        _dynamicModelLabels[m.id]=m.id;
      }
      // Hydrate the label map from extra_models too (the catalog tail that
      // doesn't render as <option> entries when the picker is capped — see
      // _build_nous_featured_set in api/config.py for the rationale). This
      // keeps a model selected from the slash-command autocomplete or a
      // persisted-localStorage value renderable with its proper label
      // instead of falling back to the bare ID. #1567.
      if(Array.isArray(g.extra_models)){
        for(const m of g.extra_models){
          if(m && m.id) _dynamicModelLabels[m.id]=m.id;
        }
      }
      sel.appendChild(og);
    }
    _reconcileModelDropdownSelection(sel,data,previousSelection,opts);
    if(typeof syncModelChip==='function') syncModelChip();
    const dd=$('composerModelDropdown');
    if(dd&&dd.classList.contains('open')&&typeof renderModelDropdown==='function'){
      renderModelDropdown();
      _positionModelDropdown();
    }
    // Kick off a background live-model fetch for the active provider.
    // This runs after the static list is already shown (no blocking flicker).
    if(data.active_provider) _fetchLiveModels(data.active_provider, sel);
  }catch(e){
    // API unavailable -- keep the hardcoded HTML options as fallback
    console.warn('Failed to load models from server:',e.message);
    if(typeof syncModelChip==='function') syncModelChip();
  }
}

// Cache so we don't re-fetch on every page load
const _liveModelCache={};
// Tracks providers for which a live-model fetch is in flight.
// Used by syncTopbar() to defer model corrections until the fetch completes,
// preventing premature fallback to the first static model (#1169).
const _liveModelFetchPending=new Set();

function _addLiveModelsToSelect(provider, models, sel){
  if(!provider||!models||!models.length||!sel) return 0;
  const currentVal=sel.value;
  let providerGroup=null;
  for(const og of sel.querySelectorAll('optgroup')){
    if(og.dataset.provider&&og.dataset.provider===provider){
      providerGroup=og; break;
    }
    if(og.label&&og.label.toLowerCase().includes(provider.toLowerCase())){
      providerGroup=og; break;
    }
  }
  if(!providerGroup){
    providerGroup=document.createElement('optgroup');
    providerGroup.label=provider.charAt(0).toUpperCase()+provider.slice(1)+' (live)';
    providerGroup.dataset.provider=provider;
    sel.appendChild(providerGroup);
  }else if(!providerGroup.dataset.provider){
    providerGroup.dataset.provider=provider;
  }
  const existingIds=new Set([...sel.options].map(o=>o.value));
  // Normalized dedup strips provider/custom prefixes and namespaces (#907, #3478).
  const _normId=id=>{
    let s=String(id||'');
    if(s.startsWith('@')&&s.includes(':')){
      if(s.startsWith('@custom:')){
        s=s.substring(s.lastIndexOf(':')+1)||s;
      }else{
        s=s.substring(s.indexOf(':')+1);
      }
    }
    s=s.split('/').pop();
    return s.replace(/-/g,'.').toLowerCase();
  };
  const existingNorm=new Set([...sel.options].map(o=>_normId(o.value)));
  let added=0;
  const _ap=(window._activeProvider||'').toLowerCase();
  const _providerLower=String(provider||'').toLowerCase();
  const _isNamedCustomActiveProvider=_ap.startsWith('custom:');
  const _isPortalFetch=_ap && _ap!=='openrouter' && _ap!=='custom' && _ap!=='openai-codex' && (_providerLower===_ap||_isNamedCustomActiveProvider&&_providerLower===_ap);
  for(const m of models){
    let mid=m.id;
    if(_isPortalFetch && !mid.startsWith('@')){
      mid=`@${provider}:${mid}`;
    }
    if(existingIds.has(mid)) continue;
    if(existingNorm.has(_normId(mid))) continue; // dedup cross-prefix duplicates (#907)
    const opt=document.createElement('option');
    opt.value=mid;
    opt.textContent=m.label||m.id;
    opt.title='Live model — fetched from provider';
    opt.dataset.provider=provider;
    providerGroup.appendChild(opt);
    _dynamicModelLabels[mid]=m.label||m.id;
    added++;
  }
  const currentProvider=(S.session&&S.session.model_provider)||null;
  if(added>0 && currentVal) _applyModelToDropdown(currentVal, sel, currentProvider);
  // After live models are added, re-apply the session's model in case it was
  // absent from the static list and syncTopbar() fired before the live fetch
  // completed (#1169). This ensures the session model wins over any premature
  // fallback that may have set sel.value to the first available option.
  if(S.session && S.session.model && sel.id==='modelSelect'){
    const reapplied=_applyModelToDropdown(S.session.model, sel, S.session.model_provider||null);
    if(reapplied && typeof syncModelChip==='function') syncModelChip();
  }
  return added;
}

async function _fetchLiveModels(provider, sel){
  if(!provider||!sel) return;
  // Already fetched — apply cached models to this select element (#872)
  if(_liveModelCache[provider]){
    const added=_addLiveModelsToSelect(provider,_liveModelCache[provider],sel);
    if(added>0 && typeof syncModelChip==='function') syncModelChip();
    return;
  }
  _liveModelFetchPending.add(provider);
  try{
    const url=new URL('api/models/live',document.baseURI||location.href);
    url.searchParams.set('provider',provider);
    const _liveRes=await fetch(url.href,{credentials:'include'});
    if(_redirectIfUnauth(_liveRes)) return;
    const data=await _liveRes.json();
    if(!data.models||!data.models.length) return;
    _liveModelCache[provider]=data.models;
    const added=_addLiveModelsToSelect(provider,data.models,sel);
    if(added>0){
      if(typeof syncModelChip==='function') syncModelChip();
      console.debug('[hermes] Live models loaded for',provider+':',added,'new models added');
    }
  }catch(e){
    console.debug('[hermes] Live model fetch failed for',provider,e.message);
  }finally{
    _liveModelFetchPending.delete(provider);
  }
}

/**
 * Check if the given model ID belongs to a different provider than the one
 * currently configured in Hermes. Returns a warning string if mismatched,
 * or null if the selection looks compatible.
 *
 * Provider detection is intentionally loose — we compare the model's slash
 * prefix (e.g. "openai/" from "openai/gpt-4o") against the active provider
 * name. Custom/local endpoints report active_provider='custom', a named
 * custom provider such as 'custom:zenmux', or the base_url hostname; skip the
 * check for those values to avoid false positives.
 */
function _checkProviderMismatch(modelId){
  const ap=(window._activeProvider||'').toLowerCase();
  if(_providerSkipsModelMismatchWarning(ap)) return null; // can't reliably check
  // @provider: prefixed IDs came from that provider's live model list — no mismatch possible
  if(modelId.startsWith('@')) return null;
  const slash=modelId.indexOf('/');
  if(slash<0) return null; // bare model name, no provider prefix
  const modelProvider=modelId.substring(0,slash).toLowerCase();
  // Normalise common aliases
  const aliases={'claude':'anthropic','gpt':'openai','gemini':'google'};
  const norm=p=>aliases[p]||p;
  if(norm(modelProvider)!==norm(ap)){
    return (window.t?window.t('provider_mismatch_warning',modelId,ap):
      `"${modelId}" may not work with your configured provider (${ap}). Send anyway or run \`hermes model\` to switch.`);
  }
  return null;
}

function _selectedModelOption(){
  const sel=$('modelSelect');
  if(!sel) return null;
  return sel.options[sel.selectedIndex]||null;
}

function _normalizeConfiguredModelKey(modelId){
  let s=String(modelId||'').trim().toLowerCase();
  // Strip @provider: prefix (e.g., @custom:jingdong:GLM-5 -> GLM-5).
  // Defensive: trailing-colon / trailing-slash falls back to the original key
  // so malformed configs don't collapse distinct ids to '' (matches backend _norm_model_id).
  if(s.startsWith('@')&&s.includes(':')){const last=s.split(':').pop();s=last||s;}
  // Skip slash-based stripping for URI-scheme IDs (e.g. gpt://folder/model)
  // whose slashes are path separators, not provider delimiters (#3429).
  const _hasScheme=/^[a-z][a-z0-9+.-]*:\/\//i.test(s);
  if(!_hasScheme){
    // Strip provider-qualified prefixes that contain colons before the first
    // slash (e.g. 'custom:llm-proxy/model' → 'model').  Without this, badge-
    // key variants like 'custom:llm-proxy/opencode_go/deepseek-v4-pro' and the
    // bare 'opencode_go/deepseek-v4-pro' produce different normalized keys and
    // aren't deduped in the configured section (#3360).
    if(s.includes('/')&&s.indexOf(':')!==-1&&s.indexOf(':')<s.indexOf('/')){
      s=s.slice(s.indexOf('/')+1)||s;
    }
    // Strip only the first slash-segment (provider prefix), preserving any
    // remaining vendor hierarchy. Using split('/').pop() here previously
    // discarded ALL segments except the last, collapsing distinct multi-slash
    // IDs like 'vendor_a/deepseek-v4-pro' and 'vendor_b/deepseek/deepseek-v4-pro'
    // to the same key, causing badge misattribution and configured-entry
    // suppression (#3360).
    if(s.includes('/')) s=s.replace(/^[^/]+\//, '')||s;
  }
  return s.replace(/-/g,'.');
}

function _getConfiguredModelBadge(modelId,badgeMap,providerId){
  const map=badgeMap||window._configuredModelBadges||{};
  if(!modelId||!map) return null;
  const provider=String(providerId||'').toLowerCase();
  const exact=map[modelId];
  if(exact && (!provider || !exact.provider || String(exact.provider).toLowerCase()===provider)) return exact;
  const targetNorm=_normalizeConfiguredModelKey(modelId);
  const matches=[];
  for(const [candidate,badge] of Object.entries(map)){
    if(_normalizeConfiguredModelKey(candidate)===targetNorm) matches.push(badge);
  }
  if(!matches.length) return null;
  if(provider){
    const providerMatch=matches.find(badge=>String(badge&&badge.provider||'').toLowerCase()===provider);
    if(providerMatch) return providerMatch;
    return matches.length===1 ? matches[0] : null;
  }
  return matches[0];
}

function syncModelChip(){
  const sel=$('modelSelect');
  const chip=$('composerModelChip');
  const label=$('composerModelLabel');
  const mobileLabel=$('composerMobileModelLabel');
  const mobileAction=$('composerMobileModelAction');
  const dd=$('composerModelDropdown');
  if(!sel||!chip||!label) return;
  // Don't show a model label until boot has finished loading to prevent flash of wrong default
  if(!S._bootReady){
    label.textContent='';
    if(mobileLabel) mobileLabel.textContent='';
    chip.title='Conversation model';
    return;
  }
  const opt=_selectedModelOption();
  const text=opt?opt.textContent:getModelLabel(sel.value||'');
  const gatewayRouting=_latestGatewayRoutingForSession(S.session);
  const displayText=_formatGatewayModelLabel(sel.value||'',text,gatewayRouting)||text;
  label.textContent=displayText;
  if(mobileLabel) mobileLabel.textContent=displayText;
  chip.title=gatewayRouting?`${sel.value||'Conversation model'} ${_gatewayRoutingLabel(gatewayRouting)}`:(sel.value||'Conversation model');
  chip.classList.toggle('active',!!(dd&&dd.classList.contains('open')));
  if(mobileAction) mobileAction.classList.toggle('active',!!(dd&&dd.classList.contains('open')));
}

function _positionModelDropdown(){
  const dd=$('composerModelDropdown');
  const chip=$('composerModelChip');
  const mobileAction=$('composerMobileModelAction');
  const footer=document.querySelector('.composer-footer');
  if(!dd||!footer) return;
  const panel=$('composerMobileConfigPanel');
  const anchor=(panel&&panel.classList.contains('open')&&mobileAction)?mobileAction:(chip&&chip.offsetParent?chip:mobileAction);
  if(!anchor) return;
  const chipRect=anchor.getBoundingClientRect();
  const footerRect=footer.getBoundingClientRect();
  let left=chipRect.left-footerRect.left;
  const maxLeft=Math.max(0, footer.clientWidth-dd.offsetWidth);
  left=Math.max(0, Math.min(left, maxLeft));
  dd.style.left=`${left}px`;
}

function renderModelDropdown(){
  const dd=$('composerModelDropdown');
  const sel=$('modelSelect');
  if(!dd||!sel) return;
  // Store model data for filtering
  const _modelData=[];
  const _badgeMap=window._configuredModelBadges||{};
  for(const child of Array.from(sel.children)){
    if(child.tagName==='OPTGROUP'){
      const providerId=child.dataset&&child.dataset.provider?child.dataset.provider:'';
      let modelsEndpointError=null;
      if(child.dataset&&child.dataset.modelsEndpointError){
        try{ modelsEndpointError=JSON.parse(child.dataset.modelsEndpointError); }catch(_e){ modelsEndpointError=null; }
      }
      for(const opt of Array.from(child.children)){
        const rawValue=String(opt.value||'');
        const displayName=rawValue.startsWith('@custom:')
          ? getModelLabel(rawValue)
          : (opt.textContent||getModelLabel(rawValue));
        _modelData.push({value:opt.value,name:esc(displayName),id:esc(opt.value),group:child.label||'',providerId,modelsEndpointError,badge:_getConfiguredModelBadge(opt.value,_badgeMap,providerId)});
      }
      if(modelsEndpointError && !child.children.length){
        _modelData.push({value:`__models_endpoint_error__:${providerId||child.label||''}`,name:'',id:'',group:child.label||'',providerId,modelsEndpointError,endpointErrorOnly:true});
      }
    }
    if(child.tagName==='OPTION'){
      const rawValue=String(child.value||'');
      const displayName=rawValue.startsWith('@custom:')
        ? getModelLabel(rawValue)
        : (child.textContent||getModelLabel(rawValue));
      _modelData.push({value:child.value,name:esc(displayName),id:esc(child.value),group:'',badge:_getConfiguredModelBadge(child.value,_badgeMap)});
    }
  }
  const _existingConfiguredKeys=new Set(_modelData.map(existing=>_normalizeConfiguredModelKey(existing.value)));
  for(const [modelId,badge] of Object.entries(_badgeMap)){
    if(_existingConfiguredKeys.has(_normalizeConfiguredModelKey(modelId))) continue;
    _modelData.push({
      value:modelId,
      name:esc(getModelLabel(modelId)),
      id:esc(modelId),
      group:'',
      badge,
    });
    _existingConfiguredKeys.add(_normalizeConfiguredModelKey(modelId));
  }
  // Create search input FIRST before filterModels definition
  const _scopeNote=document.createElement('div');
  _scopeNote.className='model-scope-note';
  _scopeNote.textContent=t('model_scope_advisory')||'Applies to this conversation from your next message.';
  const _searchRow=document.createElement('div');
  _searchRow.className='model-search-row';
  _searchRow.innerHTML=`<input class="model-search-input" type="text" placeholder="${esc(t('model_search_placeholder')||'Search models…')}" spellcheck="false" autocomplete="off"><button class="model-search-clear" title="Clear search">${li('x',10)}</button>`;
  const _si=_searchRow.querySelector('.model-search-input');
  const _sc=_searchRow.querySelector('.model-search-clear');
  // Create custom model section elements
  const _custSep=document.createElement('div');
  _custSep.className='model-group model-custom-sep';
  _custSep.textContent=t('model_custom_label')||'Custom model ID';
  const _custRow=document.createElement('div');
  _custRow.className='model-custom-row';
  _custRow.innerHTML=`<input class="model-custom-input" type="text" placeholder="${esc(t('model_custom_placeholder')||'e.g. openai/gpt-5.4')}" spellcheck="false" autocomplete="off"><button class="model-custom-btn" title="Use this model">${li('plus',12)}</button>`;
  const _ci=_custRow.querySelector('.model-custom-input');
  const _cb=_custRow.querySelector('.model-custom-btn');
  const _configuredRank=(badge)=>{
    if(!badge) return Number.POSITIVE_INFINITY;
    if(badge.role==='primary') return 0;
    if(badge.role==='fallback'){
      const m=String(badge.label||'').match(/fallback\s+(\d+)/i);
      return m?Number(m[1]):999;
    }
    return 500;
  };
  // Filter function (defined AFTER _searchRow and _cust* are created)
  const _filterModels=(term)=>{
    term=term.trim().toLowerCase();
    const found=new Set();
    for(const m of _modelData){
      const name=m.name.toLowerCase();
      const id=m.id.toLowerCase();
      if(name.includes(term)||id.includes(term)){
        found.add(m.value);
      }
    }
    const matches=(m)=>!term||found.has(m.value);
    const configuredCandidates=_modelData
      .filter(m=>m.badge&&matches(m));
    const configuredBySemanticKey=new Map();
    const _configuredProviderKey=(m)=>String((m&&m.badge&&m.badge.provider)||_providerFromModelValue(m&&m.value)||'').toLowerCase();
    const _configuredModelKey=(m)=>_normalizeConfiguredModelKey(m&&m.value||'');
    const _configuredDisplayPriority=(m)=>{
      // Prefer plain IDs over provider-qualified aliases for readability.
      const v=String((m&&m.value)||'');
      if(v.startsWith('@')) return 0;
      if(v.includes('/')) return 1;
      return 2;
    };
    for(const candidate of configuredCandidates){
      const semanticKey=`${_configuredProviderKey(candidate)}::${_configuredModelKey(candidate)}`;
      const existing=configuredBySemanticKey.get(semanticKey);
      if(!existing){
        configuredBySemanticKey.set(semanticKey,candidate);
        continue;
      }
      const candidatePriority=_configuredDisplayPriority(candidate);
      const existingPriority=_configuredDisplayPriority(existing);
      if(candidatePriority>existingPriority){
        configuredBySemanticKey.set(semanticKey,candidate);
      }
    }
    const configuredModels=[...configuredBySemanticKey.values()]
      .sort((a,b)=>{
        const configuredRankA=_configuredRank(a.badge);
        const configuredRankB=_configuredRank(b.badge);
        if(configuredRankA!==configuredRankB) return configuredRankA-configuredRankB;
        return a.name.localeCompare(b.name);
      });
    const configuredIds=new Set(configuredModels.map(m=>m.value));
    // Clear and rebuild
    dd.innerHTML='';
    // Add search and custom elements first (CRITICAL: must be before models)
    dd.appendChild(_scopeNote);
    dd.appendChild(_searchRow);
    dd.appendChild(_custSep);
    dd.appendChild(_custRow);
    if(configuredModels.length){
      const configuredHeading=document.createElement('div');
      configuredHeading.className='model-group';
      configuredHeading.textContent=t('model_group_configured')||'Configured';
      dd.appendChild(configuredHeading);
      // 为了显示原始ID，建立 badgeKeyMap: badge对象->原始key
      const badgeKeyMap = new Map();
      for(const [k, v] of Object.entries(_badgeMap)){
        badgeKeyMap.set(v, k);
      }
      for(const m of configuredModels){
        const row=document.createElement('div');
        row.className='model-opt'+(m.value===sel.value?' active':'');
        let badgeLabel = '';
        let modelName = m.name;
        if (m.badge) {
          // 直接用badge的原始key（即config.yaml里的ID）
          const rawId = badgeKeyMap.get(m.badge) || m.value || m.badge.label || 'Configured';
          badgeLabel = rawId;
          modelName = rawId; // model-opt-name直接用原始ID
          if(m.badge.provider){
            const providerName=m.badge.provider.replace(/^custom:/,'').split('/')[0];
            badgeLabel += ` (${providerName})`;
          }
        }
        const badgeHtml=m.badge?`<span class="model-opt-badge model-opt-badge--${esc(m.badge.role||'configured')}">${esc(badgeLabel)}</span>`:'';
        row.innerHTML=`<div class="model-opt-top"><span class="model-opt-name">${esc(modelName)}</span>${badgeHtml}</div><span class="model-opt-id">${esc(m.id)}</span>`;
        row.onclick=()=>selectModelFromDropdown(m.value,(m.badge&&m.badge.provider)||m.providerId||null);
        dd.appendChild(row);
      }
    }
    // Add remaining models matching filter
    let _lastGroup=null;
    // Count models per group for heading labels (#1425)
    const _groupCounts={};
    for(const m of _modelData){
      if(configuredIds.has(m.value)) continue;
      if(m.group&&!m.endpointErrorOnly) _groupCounts[m.group]=(_groupCounts[m.group]||0)+1;
    }
    const _renderProviderEndpointHint=(groupName)=>{
      if(!groupName) return;
      const entry=_modelData.find(m=>m.group===groupName&&m.modelsEndpointError);
      if(!entry||!entry.modelsEndpointError) return;
      const hint=document.createElement('div');
      hint.className='model-provider-hint';
      hint.textContent=entry.modelsEndpointError.message||'Models endpoint could not be reached for this provider.';
      dd.appendChild(hint);
    };
    for(const m of _modelData){
      if(configuredIds.has(m.value)||!matches(m)) continue;
      if(m.group&&m.group!==_lastGroup){
        const heading=document.createElement('div');
        heading.className='model-group';
        const count=_groupCounts[m.group]||0;
        heading.textContent=count>1?`${m.group} (${count})`:m.group;
        dd.appendChild(heading);
        _renderProviderEndpointHint(m.group);
        _lastGroup=m.group;
      }
      if(m.endpointErrorOnly) continue;
      const row=document.createElement('div');
      row.className='model-opt'+(m.value===sel.value?' active':'');
      const badgeHtml=m.badge?`<span class="model-opt-badge model-opt-badge--${esc(m.badge.role||'configured')}">${esc(m.badge.label||'Configured')}</span>`:'';
      // Inline provider chip on every row that has a group (#1425)
      const providerChip=m.group?`<span class="model-opt-provider">${esc(m.group)}</span>`:'';
      row.innerHTML=`<div class="model-opt-top"><span class="model-opt-name">${esc(m.name)}</span>${badgeHtml}${providerChip}</div><span class="model-opt-id">${esc(m.id)}</span>`;
      row.onclick=()=>selectModelFromDropdown(m.value,m.providerId||(m.badge&&m.badge.provider)||null);
      dd.appendChild(row);
    }
    // Show "No results" if filtered and nothing matched
    if(term&&found.size===0){
      const noResult=document.createElement('div');
      noResult.className='model-search-no-results';
      noResult.textContent=t('model_search_no_results')||'No models found';
      noResult.style.padding='12px 14px';
      noResult.style.color='var(--muted)';
      noResult.style.textAlign='center';
      dd.appendChild(noResult);
    }
    // Restore focus to search input
    _si.focus();
  };
  // Event handlers for search input
  _si.addEventListener('input',()=>_filterModels(_si.value));
  _si.addEventListener('keydown',e=>{if(e.key==='Enter') {e.preventDefault();}if(e.key==='Escape') {closeModelDropdown();}});
  _si.addEventListener('click',e=>e.stopPropagation());
  // Event handlers for clear button
  _sc.onclick=()=>{ _si.value=''; _filterModels(''); _si.focus(); };
  _sc.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){ _si.value=''; _filterModels(''); _si.focus(); e.preventDefault(); }});
  // Event handlers for custom input
  const _applyCustom=()=>{const v=_ci.value.trim();if(!v)return;selectModelFromDropdown(v);_ci.value='';};
  _cb.onclick=_applyCustom;
  _ci.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();_applyCustom();}if(e.key==='Escape'){closeModelDropdown();}});
  _ci.addEventListener('click',e=>e.stopPropagation());
  // Add search and custom elements to dropdown (initial render)
  dd.appendChild(_scopeNote);
  dd.appendChild(_searchRow);
  dd.appendChild(_custSep);
  dd.appendChild(_custRow);
  // Apply initial filter (empty shows all)
  _filterModels('');
}

async function selectModelFromDropdown(value){
  const preferredProviderId=arguments[1];
  const sel=$('modelSelect');
  if(!sel) { closeModelDropdown(); return; }
  const provider=String(preferredProviderId||'').trim()||null;
  const currentState=(typeof _modelStateForSelect==='function')
    ? _modelStateForSelect(sel, sel.value)
    : {model:sel.value,model_provider:null};
  const sameModel=String(currentState.model||'')===String(value||'');
  const sameProvider=String(currentState.model_provider||'')===String(provider||'');
  if(sameModel&&sameProvider){ closeModelDropdown(); return; }
  // Resolve the provider-specific option so duplicate bare IDs (e.g. gpt-5.5
  // under OpenAI Codex vs OpenRouter) update session model_provider correctly.
  if(typeof _ensureModelOptionInDropdown==='function'){
    _ensureModelOptionInDropdown(value, sel, provider);
  }else{
    sel.value=value;
  }
  syncModelChip();
  closeModelDropdown();
  if(typeof sel.onchange==='function') await sel.onchange();
}

async function toggleModelDropdown(){
  const dd=$('composerModelDropdown');
  const chip=$('composerModelChip');
  const sel=$('modelSelect');
  if(!dd||!chip||!sel) return;
  const open=dd.classList.contains('open');
  if(open){closeModelDropdown(); return;}
  if(typeof closeProfileDropdown==='function') closeProfileDropdown();
  if(typeof closeWsDropdown==='function') closeWsDropdown();
  if(typeof closeReasoningDropdown==='function') closeReasoningDropdown();
  if(typeof closeToolsetsDropdown==='function') closeToolsetsDropdown();
  if(typeof window._ensureModelDropdownReady==='function'){
    const ready=window._ensureModelDropdownReady();
    if(ready&&typeof ready.catch==='function') ready.catch(()=>{});
  }
  if(dd.classList.contains('open')) return;
  renderModelDropdown();
  dd.classList.add('open');
  _positionModelDropdown();
  chip.classList.add('active');
  const mobileAction=$('composerMobileModelAction');
  if(mobileAction) mobileAction.classList.add('active');
}

function closeModelDropdown(){
  const dd=$('composerModelDropdown');
  const chip=$('composerModelChip');
  const mobileAction=$('composerMobileModelAction');
  if(dd) dd.classList.remove('open');
  if(chip) chip.classList.remove('active');
  if(mobileAction) mobileAction.classList.remove('active');
}

document.addEventListener('click',e=>{
  if(
    !e.target.closest('#composerModelChip') &&
    !e.target.closest('#composerMobileModelAction') &&
    !e.target.closest('#composerModelDropdown')
  ) closeModelDropdown();
});
window.addEventListener('resize',()=>{
  const dd=$('composerModelDropdown');
  if(dd&&dd.classList.contains('open')) _positionModelDropdown();
  // Keep the reasoning dropdown aligned under its chip when the window
  // resizes while open — same pattern as the model dropdown above.
  const rdd=$('composerReasoningDropdown');
  if(rdd&&rdd.classList.contains('open')&&typeof _positionReasoningDropdown==='function'){
    _positionReasoningDropdown();
  }
});

// ── Reasoning effort chip ────────────────────────────────────────────────────
let _currentReasoningEffort=null;
let _currentReasoningEffortsSupported=null;

function _normalizeReasoningEffort(eff){
  return String(eff||'').trim().toLowerCase();
}

function _formatReasoningEffortLabel(effort){
  if(effort==='none') return 'None';
  if(!effort) return 'Default';
  return effort;
}

function _reasoningEffortQuery(){
  const sel=$('modelSelect');
  const model=(S&&S.session&&S.session.model)||(sel&&sel.value)||'';
  let provider=(S&&S.session&&S.session.model_provider)||'';
  if(!provider&&sel&&model&&typeof _modelStateForSelect==='function'){
    provider=_modelStateForSelect(sel, model).model_provider||'';
  }
  const params=new URLSearchParams();
  if(model) params.set('model', model);
  if(provider) params.set('provider', provider);
  const qs=params.toString();
  return qs?('?'+qs):'';
}

function _applyReasoningOptions(supportedEfforts){
  const dd=$('composerReasoningDropdown');
  if(!dd) return;
  const supported=new Set(Array.isArray(supportedEfforts)?supportedEfforts:[]);
  dd.querySelectorAll('.reasoning-option').forEach(function(opt){
    const effort=opt.dataset.effort;
    if(effort==='none'){
      opt.style.display='';
      return;
    }
    if(!supported.size){
      opt.style.display='none';
      return;
    }
    opt.style.display=supported.has(effort)?'':'none';
  });
}

function _applyReasoningChip(eff){
  const meta=arguments[1]||null;
  const effort=_normalizeReasoningEffort(eff);
  _currentReasoningEffort=effort;
  if(meta&&Array.isArray(meta.supported_efforts)){
    _currentReasoningEffortsSupported=meta.supported_efforts;
  }
  const wrap=$('composerReasoningWrap');
  const label=$('composerReasoningLabel');
  const chip=$('composerReasoningChip');
  const mobileLabel=$('composerMobileReasoningLabel');
  const mobileAction=$('composerMobileReasoningAction');
  if(!wrap||!label) return;
  const supportedEfforts=(typeof _currentReasoningEffortsSupported==='undefined')
    ?null
    :_currentReasoningEffortsSupported;
  const supports=Array.isArray(supportedEfforts)
    ?supportedEfforts.length>0
    :true;
  if(!supports){
    wrap.style.display='none';
    if(mobileAction) mobileAction.style.display='none';
    return;
  }
  wrap.style.display='';
  if(mobileAction) mobileAction.style.display='';
  if(typeof _applyReasoningOptions==='function') _applyReasoningOptions(supportedEfforts);
  const text=_formatReasoningEffortLabel(effort);
  label.textContent=text;
  if(mobileLabel) mobileLabel.textContent=text;
  if(chip){
    const inactive=!effort||effort==='none';
    chip.classList.toggle('inactive',inactive);
    chip.title='Reasoning effort: '+text;
  }
  if(mobileAction) mobileAction.classList.toggle('inactive',!effort||effort==='none');
  _highlightReasoningOption(effort);
}

function fetchReasoningChip(){
  api('/api/reasoning'+_reasoningEffortQuery()).then(function(st){
    _applyReasoningChip((st&&st.reasoning_effort)||'', st||{});
  }).catch(function(){_applyReasoningChip('', {supported_efforts:[]});});
}

function syncReasoningChip(){
  fetchReasoningChip();
}

function _highlightReasoningOption(effort){
  const dd=$('composerReasoningDropdown');
  if(!dd) return;
  dd.querySelectorAll('.reasoning-option').forEach(function(opt){
    opt.classList.toggle('selected',opt.dataset.effort===effort);
  });
}

function toggleReasoningDropdown(){
  const dd=$('composerReasoningDropdown');
  const chip=$('composerReasoningChip');
  if(!dd||!chip) return;
  const open=dd.classList.contains('open');
  if(open){closeReasoningDropdown();return;}
  if(typeof closeProfileDropdown==='function') closeProfileDropdown();
  if(typeof closeWsDropdown==='function') closeWsDropdown();
  closeModelDropdown();
  if(typeof closeToolsetsDropdown==='function') closeToolsetsDropdown();
  _highlightReasoningOption(_currentReasoningEffort);
  dd.classList.add('open');
  _positionReasoningDropdown();
  chip.classList.add('active');
  const mobileAction=$('composerMobileReasoningAction');
  if(mobileAction) mobileAction.classList.add('active');
}

function _positionReasoningDropdown(){
  const dd=$('composerReasoningDropdown');
  const chip=$('composerReasoningChip');
  const mobileAction=$('composerMobileReasoningAction');
  const footer=document.querySelector('.composer-footer');
  if(!dd||!chip||!footer) return;
  const panel=$('composerMobileConfigPanel');
  const anchor=(panel&&panel.classList.contains('open')&&mobileAction)?mobileAction:chip;
  const chipRect=anchor.getBoundingClientRect();
  const footerRect=footer.getBoundingClientRect();
  let left=chipRect.left-footerRect.left;
  const maxLeft=Math.max(0,footer.clientWidth-dd.offsetWidth);
  left=Math.max(0,Math.min(left,maxLeft));
  dd.style.left=`${left}px`;
}

function closeReasoningDropdown(){
  const dd=$('composerReasoningDropdown');
  const chip=$('composerReasoningChip');
  const mobileAction=$('composerMobileReasoningAction');
  if(dd) dd.classList.remove('open');
  if(chip) chip.classList.remove('active');
  if(mobileAction) mobileAction.classList.remove('active');
}

document.addEventListener('click',function(e){
  if(
    !e.target.closest('#composerReasoningChip') &&
    !e.target.closest('#composerMobileReasoningAction') &&
    !e.target.closest('#composerReasoningDropdown')
  ) closeReasoningDropdown();
  if(e.target.closest('.reasoning-option')){
    const opt=e.target.closest('.reasoning-option');
    const effort=opt&&opt.dataset.effort;
    if(effort){
      api('/api/reasoning',{method:'POST',body:JSON.stringify({effort:effort})})
        .then(function(st){
          _applyReasoningChip((st&&st.reasoning_effort)||effort, st||{});
          showToast('🧠 Reasoning effort set to '+((st&&st.reasoning_effort)||effort));
        })
        .catch(function(){showToast('🧠 Failed to set effort');});
      closeReasoningDropdown();
    }
  }
});

// ── Session toolsets chip (#493) ───────────────────────────────────────────
let _currentSessionToolsets = null; // null = global, array = custom list

function _applyToolsetsChip(toolsets) {
  _currentSessionToolsets = toolsets;
  const wrap = $('composerToolsetsWrap');
  const label = $('composerToolsetsLabel');
  const chip = $('composerToolsetsChip');
  if (!wrap || !label) return;
  // Visibility is controlled entirely by responsive CSS — the chip shows only
  // at wide composer-footer widths (>= 1100px container query). At narrower
  // widths the layout is too cramped (model + reasoning + profile + workspace
  // + context-ring + send) to add another chip. Cleared inline style so the
  // CSS @container query is the single source of truth. State is still
  // tracked so /api/session/toolsets continues to work for cron/scripted
  // callers regardless of UI visibility. (#1431)
  wrap.style.display = '';
  const hasCustom = Array.isArray(toolsets) && toolsets.length > 0;
  if (hasCustom) {
    label.textContent = toolsets.join(', ');
    chip.classList.add('has-custom');
    chip.title = t('session_toolsets') + ': ' + toolsets.join(', ');
  } else {
    label.textContent = t('session_toolsets_global');
    chip.classList.remove('has-custom');
    chip.title = t('session_toolsets');
  }
}

function _syncToolsetsChip() {
  if (typeof S === 'undefined' || !S || !S.session) {
    _applyToolsetsChip(null);
    return;
  }
  _applyToolsetsChip(S.session.enabled_toolsets || null);
}

function syncToolsetsChip() {
  _syncToolsetsChip();
}

function _populateToolsetsDropdown() {
  const desc = $('toolsetsDropdownDesc');
  const state = $('toolsetsDropdownState');
  const input = $('toolsetsInput');
  const applyBtn = $('toolsetsApplyBtn');
  const clearBtn = $('toolsetsClearBtn');
  if (!desc || !state || !input) return;
  desc.textContent = t('session_toolsets_desc');
  if (applyBtn) applyBtn.textContent = t('session_toolsets_apply');
  if (clearBtn) clearBtn.textContent = t('session_toolsets_clear');
  input.placeholder = t('session_toolsets_placeholder');
  // Escape key handler for toolsets input
  input.onkeydown = function(e) { if(e.key === 'Escape') closeToolsetsDropdown(); };
  const hasCustom = Array.isArray(_currentSessionToolsets) && _currentSessionToolsets.length > 0;
  if (hasCustom) {
    state.textContent = '🔧 ' + _currentSessionToolsets.join(', ');
    input.value = _currentSessionToolsets.join(', ');
  } else {
    state.textContent = '🌍 ' + t('session_toolsets_global');
    input.value = '';
  }
}

function _positionToolsetsDropdown() {
  const dd = $('composerToolsetsDropdown');
  const chip = $('composerToolsetsChip');
  const footer = document.querySelector('.composer-footer');
  if (!dd || !chip || !footer) return;
  // Defense: if the chip has been hidden by responsive CSS (e.g. resize across
  // 1100px container threshold while dropdown was open), don't try to anchor
  // to a zero-rect element — close the dropdown instead. (#1431)
  if (chip.offsetParent === null) { closeToolsetsDropdown(); return; }
  const chipRect = chip.getBoundingClientRect();
  const footerRect = footer.getBoundingClientRect();
  let left = chipRect.left - footerRect.left;
  const maxLeft = Math.max(0, footer.clientWidth - dd.offsetWidth);
  left = Math.max(0, Math.min(left, maxLeft));
  dd.style.left = left + 'px';
}

function toggleToolsetsDropdown() {
  const dd = $('composerToolsetsDropdown');
  const chip = $('composerToolsetsChip');
  if (!dd || !chip) return;
  if (typeof S === 'undefined' || !S || !S.session) return;
  // Don't open when the chip itself is hidden by responsive CSS (#1431).
  // offsetParent === null catches display:none on the element or any ancestor.
  if (chip.offsetParent === null) return;
  const open = dd.classList.contains('open');
  if (open) { closeToolsetsDropdown(); return; }
  if (typeof closeProfileDropdown === 'function') closeProfileDropdown();
  if (typeof closeWsDropdown === 'function') closeWsDropdown();
  closeModelDropdown();
  if (typeof closeReasoningDropdown === 'function') closeReasoningDropdown();
  _syncToolsetsChip();
  _populateToolsetsDropdown();
  dd.classList.add('open');
  _positionToolsetsDropdown();
  chip.classList.add('active');
  // Focus the input after a tick so the layout has settled
  setTimeout(() => { const inp = $('toolsetsInput'); if (inp) inp.focus(); }, 50);
}

function closeToolsetsDropdown() {
  const dd = $('composerToolsetsDropdown');
  const chip = $('composerToolsetsChip');
  if (dd) dd.classList.remove('open');
  if (chip) chip.classList.remove('active');
}

function _applySessionToolsets(toolsets) {
  if (typeof S === 'undefined' || !S || !S.session) return;
  const sid = S.session.session_id;
  api('/api/session/toolsets', {
    method: 'POST',
    body: JSON.stringify({ session_id: sid, toolsets: toolsets })
  })
    .then(function(r) {
      if (r && r.ok) {
        S.session.enabled_toolsets = r.enabled_toolsets || null;
        _applyToolsetsChip(r.enabled_toolsets || null);
        if (r.enabled_toolsets && r.enabled_toolsets.length) {
          showToast('🔧 ' + t('session_toolsets_applied') + ': ' + r.enabled_toolsets.join(', '));
        } else {
          showToast('🌍 ' + t('session_toolsets_cleared'));
        }
      } else {
        showToast(t('session_toolsets_failed') + (r && r.error ? r.error : 'Unknown error'), 3000, 'error');
      }
    })
    .catch(function(err) {
      showToast(t('session_toolsets_failed') + (err.message || err), 3000, 'error');
    });
}

// Click-outside handler for toolsets dropdown
document.addEventListener('click', function(e) {
  if (
    !e.target.closest('#composerToolsetsChip') &&
    !e.target.closest('#composerToolsetsDropdown')
  ) closeToolsetsDropdown();
  // Apply button
  if (e.target.closest('#toolsetsApplyBtn')) {
    const input = $('toolsetsInput');
    if (!input) return;
    const raw = input.value.trim();
    if (!raw) {
      showToast(t('session_toolsets_desc'), 2000);
      return;
    }
    const toolsets = raw.split(',').map(s => s.trim()).filter(Boolean);
    if (toolsets.length === 0) {
      showToast(t('session_toolsets_desc'), 2000);
      return;
    }
    _applySessionToolsets(toolsets);
    closeToolsetsDropdown();
  }
  // Clear button
  if (e.target.closest('#toolsetsClearBtn')) {
    _applySessionToolsets(null);
    closeToolsetsDropdown();
  }
});

// Position toolsets dropdown on resize, OR close it if the chip is no longer
// visible (e.g. resize crossed the 1100px container threshold while dropdown
// was open — the wrap is hidden by CSS but the dropdown sibling stays open
// without an anchor). (#1431)
window.addEventListener('resize', () => {
  const dd = $('composerToolsetsDropdown');
  if (!dd || !dd.classList.contains('open')) return;
  const chip = $('composerToolsetsChip');
  if (!chip || chip.offsetParent === null) { closeToolsetsDropdown(); return; }
  _positionToolsetsDropdown();
});

function _syncMobileComposerConfigButton(open){
  const btn=$('composerMobileConfigBtn');
  if(!btn) return;
  btn.classList.toggle('active',!!open);
  btn.setAttribute('aria-expanded',open?'true':'false');
}

function closeMobileComposerConfig(){
  const panel=$('composerMobileConfigPanel');
  if(panel) panel.classList.remove('open');
  _syncMobileComposerConfigButton(false);
  if(typeof closeWsDropdown==='function') closeWsDropdown();
}

function toggleMobileComposerConfig(){
  const panel=$('composerMobileConfigPanel');
  if(!panel) return;
  const open=panel.classList.contains('open');
  if(open){
    closeMobileComposerConfig();
    closeModelDropdown();
    closeReasoningDropdown();
    if(typeof closeToolsetsDropdown==='function') closeToolsetsDropdown();
    return;
  }
  if(typeof closeProfileDropdown==='function') closeProfileDropdown();
  if(typeof closeWsDropdown==='function') closeWsDropdown();
  closeModelDropdown();
  closeReasoningDropdown();
  if(typeof closeToolsetsDropdown==='function') closeToolsetsDropdown();
  panel.classList.add('open');
  _syncMobileComposerConfigButton(true);
}

document.addEventListener('click',function(e){
  if(
    e.target.closest('#composerMobileConfigBtn') ||
    e.target.closest('#composerMobileConfigPanel') ||
    e.target.closest('#composerWsDropdown') ||
    e.target.closest('#composerModelDropdown') ||
    e.target.closest('#composerReasoningDropdown')
  ) return;
  closeMobileComposerConfig();
});

document.addEventListener('keydown',function(e){
  if(e.key!=='Escape') return;
  const panel=$('composerMobileConfigPanel');
  if(!panel||!panel.classList.contains('open')) return;
  e.preventDefault();
  closeMobileComposerConfig();
  if(typeof closeWsDropdown==='function') closeWsDropdown();
  closeModelDropdown();
  closeReasoningDropdown();
});

window.addEventListener('resize',function(){
  if(window.matchMedia && !window.matchMedia('(max-width: 640px)').matches){
    closeMobileComposerConfig();
    closeModelDropdown();
    closeReasoningDropdown();
    if(typeof closeWsDropdown==='function') closeWsDropdown();
  }
});

// ── Scroll pinning ──────────────────────────────────────────────────────────
// When streaming, auto-scroll only while the user is following the live tail.
// Any manual scroll up sets a sticky unpinned flag until the user scrolls back
// to the bottom (near-bottom hysteresis on downward motion) or clicks ↓.
// Programmatic scrolls are ignored via _programmaticScroll. Fixes #1469 / #1360 / #1731.
let _scrollPinned=true;
let _programmaticScroll=false;
let _nearBottomCount=0;
let _lastScrollTop=null;
// Sticky-unpin model (#3343 supersedes #3330's proximity re-pin): once the user
// scrolls up, streaming stops auto-following until they return to the bottom or
// click ↓. The upward-intent TIMEOUT mechanism (_lastMessageUpwardIntentMs /
// MESSAGE_UPWARD_INTENT_MS) is removed — sticky-unpin makes it unnecessary.
// Keep the non-message intent timestamp at -Infinity so load-time isn't read as
// intent (the #3330 follow-up fix); 0 would mark the first NON_MESSAGE_SCROLL_INTENT
// window after load as suppressed.
let _lastNonMessageScrollIntentMs=-Infinity;
let _messageUserUnpinned=false;
let _bottomSettleToken=0;
const NON_MESSAGE_SCROLL_INTENT_SUPPRESS_MS=350;
let _touchStartY=null;
function _cancelBottomSettle(){ _bottomSettleToken++; }
function _recordNonMessageScrollIntent(e){
  const el=document.getElementById('messages');
  const target=e&&e.target;
  if(!el||!target) return;
  if(!el.contains(target)) _lastNonMessageScrollIntentMs=performance.now();
  else if(e.type==='touchmove'||(typeof e.deltaY==='number'&&e.deltaY<0)){
    _cancelBottomSettle();
    if(typeof e.deltaY==='number'&&e.deltaY<0){
      _messageUserUnpinned=true;
      _nearBottomCount=0;
      _scrollPinned=false;
    } else if(e.type==='touchmove'&&_touchStartY!==null&&e.touches&&e.touches[0]){
      // Detect upward-scroll intent on touch: dragging the finger DOWN the
      // screen scrolls the content up into earlier history (scrollTop
      // decreases) — the same "user scrolled away" signal the wheel deltaY<0
      // branch and the scroll listener's movedUp branch use. dy>0 = finger
      // moved down = reveal earlier content = unpin.
      const dy=e.touches[0].clientY-_touchStartY;
      if(dy>8){
        _messageUserUnpinned=true;
        _nearBottomCount=0;
        _scrollPinned=false;
      }
    }
  }
}
function _recentNonMessageScrollIntent(){
  return performance.now()-_lastNonMessageScrollIntentMs<NON_MESSAGE_SCROLL_INTENT_SUPPRESS_MS;
}
if(typeof document!=='undefined'){
  document.addEventListener('wheel',_recordNonMessageScrollIntent,{capture:true,passive:true});
  document.addEventListener('touchmove',_recordNonMessageScrollIntent,{capture:true,passive:true});
  document.addEventListener('touchstart',function(e){
    if(e.touches&&e.touches[0]) _touchStartY=e.touches[0].clientY;
  },{capture:true,passive:true});
  document.addEventListener('touchend',function(){ _touchStartY=null; },{capture:true,passive:true});
  document.addEventListener('touchcancel',function(){ _touchStartY=null; },{capture:true,passive:true});
}
// Reset hook for session-switch — called from sessions.js loadSession() to
// prevent the new chat's first scroll comparing against the previous chat's
// scrollTop (Opus stage-302 SHOULD-FIX, #1731 follow-up).
function _resetScrollDirectionTracker(){
  _lastScrollTop=null;
  _messageUserUnpinned=false;
  _scrollPinned=true;
  _nearBottomCount=0;
  _touchStartY=null;
}
function _resetStreamScrollFollow(){
  _messageUserUnpinned=false;
  _scrollPinned=true;
  _nearBottomCount=0;
  _lastScrollTop=null;
  _cancelBottomSettle();
}
if(typeof window!=='undefined'){
  window._resetScrollDirectionTracker=_resetScrollDirectionTracker;
  window._resetStreamScrollFollow=_resetStreamScrollFollow;
}
/* ── Pull-to-refresh for PWA standalone (Android) ── */
(function(){
  if(typeof document==='undefined') return;
  const isStandalone=window.navigator?.standalone||matchMedia('(display-mode:standalone),(display-mode:fullscreen)').matches;
  if(!isStandalone) return;
  const el=document.getElementById('messages');
  if(!el) return;
  let _ptrState=0; // 0=idle, 1=pulling, 2=ready
  let _ptrStartY=0;
  let _ptrCurrentY=0;
  const THRESHOLD=80;
  let _indicator=null;
  function _ptrCreateIndicator(){
    if(_indicator) return;
    _indicator=document.createElement('div');
    _indicator.className='pull-to-refresh-indicator';
    _indicator.innerHTML='<span class="ptr-icon">↓</span> <span class="ptr-text">Pull to refresh</span>';
    el.parentNode.insertBefore(_indicator,el);
  }
  function _ptrUpdate(progress){
    _ptrCreateIndicator();
    const pulling=progress<1;
    _indicator.classList.toggle('active',progress>0);
    const icon=_indicator.querySelector('.ptr-icon');
    const text=_indicator.querySelector('.ptr-text');
    if(icon) icon.classList.toggle('ready',!pulling);
    if(text) text.textContent=pulling?'Pull to refresh':'Release to refresh';
  }
  function _ptrReset(){
    _ptrState=0;
    _ptrStartY=0;
    _ptrCurrentY=0;
    if(_indicator) _indicator.classList.remove('active');
  }
  el.addEventListener('touchstart',function(e){
    if(el.scrollTop>0||_ptrState!==0) return;
    _ptrStartY=e.touches[0].clientY;
    _ptrState=1;
  },{passive:true});
  el.addEventListener('touchmove',function(e){
    if(_ptrState!==1) return;
    _ptrCurrentY=e.touches[0].clientY;
    const pull=_ptrCurrentY-_ptrStartY;
    if(pull<0){ _ptrReset(); return; }
    /* If not at the top, smooth-scroll to top first.
       Next pull gesture will trigger the refresh. */
    if(el.scrollTop>0){
      el.scrollTo({top:0,behavior:'smooth'});
      _ptrReset();
      return;
    }
    const progress=Math.min(pull/THRESHOLD,1);
    _ptrUpdate(progress);
    _ptrState=progress>=1?2:1;
    if(progress>0.3) e.preventDefault();
  },{passive:false});
  el.addEventListener('touchend',function(){
    if(_ptrState===2){
      if(typeof window.refreshSessionList==='function'){
        Promise.resolve(window.refreshSessionList('pull', {force:true, refreshActive:true})).catch(()=>{}).finally(_ptrReset);
      }else{
        window.location.reload();
      }
      return;
    }
    _ptrReset();
  },{passive:true});
  el.addEventListener('touchcancel',_ptrReset,{passive:true});
})();
(function(){
  const el=document.getElementById('messages');
  if(!el) return;
  let _scrollRaf=0;
  el.addEventListener('scroll',()=>{
    if(_programmaticScroll) return; // ignore scrolls we triggered ourselves
    cancelAnimationFrame(_scrollRaf);
    _scrollRaf=requestAnimationFrame(()=>{
      const top=el.scrollTop;
      const nearBottom=el.scrollHeight-top-el.clientHeight<250;
      const movedUp=_lastScrollTop!==null&&top<_lastScrollTop-2;
      const movedDown=_lastScrollTop!==null&&top>_lastScrollTop+2;
      _lastScrollTop=top;
      if(movedUp){
        _cancelBottomSettle();
        _nearBottomCount=0;
        _scrollPinned=false;
        _messageUserUnpinned=true;
      }else if(movedDown&&nearBottom){
        _nearBottomCount=_nearBottomCount+1;
        if(_nearBottomCount>=2){
          _scrollPinned=true;
          _messageUserUnpinned=false;
        }
      }else if(!_messageUserUnpinned){
        if(nearBottom){
          _nearBottomCount=_nearBottomCount+1;
          if(_nearBottomCount>=2) _scrollPinned=true;
        }else{
          _nearBottomCount=0;
          _scrollPinned=false;
        }
      }else if(!nearBottom){
        _nearBottomCount=0;
        _scrollPinned=false;
      }
      const btn=$('scrollToBottomBtn');
      const showBottomButton=!_scrollPinned && el.scrollHeight-top-el.clientHeight>80;
      if(btn) btn.style.display=showBottomButton?'flex':'none';
      if(typeof _updateSessionStartJumpButton==='function') _updateSessionStartJumpButton();
      // Prefetch older messages before the reader hits the hard top. Prepending
      // then preserving scrollTop is seamless only if there is runway left for
      // the user's continued upward wheel/touch movement.
      const olderPrefetchPx=Math.max(600,el.clientHeight*1.5);
      if(_isSessionEndlessScrollEnabled()&&el.scrollTop<olderPrefetchPx && typeof _messagesTruncated!=='undefined' && _messagesTruncated && typeof _loadOlderMessages==='function'){
        _loadOlderMessages();
      }
    });
  });
})();
function _fmtTokens(n){if(!n||n<0)return'0';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'k';return String(n);}
function _formatTurnDuration(seconds){
  const n=Number(seconds);
  if(!Number.isFinite(n)||n<0)return'';
  const total=Math.max(0,Math.round(n));
  if(total<60)return`${total}s`;
  const h=Math.floor(total/3600);
  const m=Math.floor((total%3600)/60);
  const s=total%60;
  if(h)return`${h}h ${m}m`;
  return`${m}m ${s}s`;
}
function _formatActiveElapsedTimer(seconds){
  const n=Number(seconds);
  if(!Number.isFinite(n)||n<0)return'';
  const total=Math.max(0,Math.floor(n));
  const m=Math.floor(total/60);
  const s=total%60;
  return`${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}
const _COMPRESSION_ELAPSED_MAX_SECONDS=5*60;
let _compressionElapsedTimer=null;
function _compressionElapsedStartedAt(state){const n=Number(state&&state.startedAt);return Number.isFinite(n)&&n>0?n:null;}
function _compressionElapsedLabel(state){
  const started=_compressionElapsedStartedAt(state);
  if(!started)return'';
  const elapsed=Math.max(0,(Date.now()/1000)-started);
  if(elapsed>=_COMPRESSION_ELAPSED_MAX_SECONDS)return '5+ min';
  return _formatActiveElapsedTimer(elapsed);
}
function _compressionElapsedExpired(state){const started=_compressionElapsedStartedAt(state);return !!(started&&((Date.now()/1000)-started)>=_COMPRESSION_ELAPSED_MAX_SECONDS);}
function _compressionLiveCardNode(){return document.querySelector('[data-live-compression-card="1"][data-compression-started-at]');}
function _compressionLiveCardState(){
  const node=_compressionLiveCardNode();
  const started=Number(node&&node.getAttribute('data-compression-started-at'));
  if(!node||!S.session||!Number.isFinite(started)||started<=0)return null;
  return {sessionId:S.session.session_id,phase:'running',automatic:true,message:node.getAttribute('data-compression-message')||'Auto-compressing context...',startedAt:started};
}
function _updateCompressionElapsedCards(state){
  if(!state)return false;
  const preview=_autoCompressionPreviewText(state), detail=_autoCompressionDetailText(state);
  let updated=false;
  document.querySelectorAll('.tool-card-compress-auto.tool-card-compress-running').forEach(card=>{
    const previewEl=card.querySelector('.tool-card-preview');
    const detailEl=card.querySelector('.tool-card-result pre');
    if(previewEl) previewEl.textContent=preview;
    if(detailEl) detailEl.textContent=detail;
    updated=true;
  });
  return updated;
}
function _updateCompressionElapsedTimer(){
  const state=_compressionStateForCurrentSession()||_compressionLiveCardState();
  if(state&&state.automatic&&state.phase==='running'){
    _updateCompressionElapsedCards(state);
    if(_compressionElapsedExpired(state)) _clearCompressionElapsedTimer();
  }else _clearCompressionElapsedTimer();
}
function _startCompressionElapsedTimer(){if(!_compressionElapsedTimer)_compressionElapsedTimer=setInterval(_updateCompressionElapsedTimer,1000);}
function _clearCompressionElapsedTimer(){if(_compressionElapsedTimer){clearInterval(_compressionElapsedTimer);_compressionElapsedTimer=null;}}
let _activityElapsedTimer=null;
let _activityElapsedTimerGroup=null;
function _activityNowSeconds(){return Date.now()/1000;}
function _activityElapsedStartedAt(group){
  if(!group)return null;
  const raw=(group.dataset&&group.dataset.turnStartedAt!==undefined&&group.dataset.turnStartedAt!=='')
    ?group.dataset.turnStartedAt
    :(S.session&&S.session.pending_started_at);
  const started=Number(raw);
  return Number.isFinite(started)&&started>0?started:null;
}
function _activityElapsedLabel(group){
  const started=_activityElapsedStartedAt(group);
  if(!started)return'';
  return _formatActiveElapsedTimer(_activityNowSeconds()-started);
}
function _activityMarkObserved(group, ts){
  if(!group||group.getAttribute('data-live-tool-call-group')!=='1')return;
  const stamp=Number(ts||_activityNowSeconds());
  if(Number.isFinite(stamp)&&stamp>0) group.setAttribute('data-last-activity-at',String(stamp));
}
function _activityLastObservedAge(group){
  const stamp=Number(group&&group.getAttribute('data-last-activity-at'));
  if(!Number.isFinite(stamp)||stamp<=0)return null;
  return Math.max(0,_activityNowSeconds()-stamp);
}
function _activityClockLabel(ts){
  const stamp=Number(ts||_activityNowSeconds());
  if(!Number.isFinite(stamp)||stamp<=0)return'';
  try{return new Date(stamp*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});}catch(_){return'';}
}
function _activityStatusNode({kind='info',label='',detail='',status='done',ts=null,id=''}){
  const row=document.createElement('div');
  row.className=`agent-activity-status agent-activity-status-${kind} agent-activity-status-${status}`;
  if(id) row.setAttribute('data-activity-event-id',id);
  if(ts) row.setAttribute('data-activity-at',String(ts));
  const iconMap={run:li('play',13),model:li('bot',13),waiting:'<span class="tool-card-running-dot"></span>',thinking:li('lightbulb',13),tool:li('wrench',13),done:li('check',13),warning:li('alert-triangle',13)};
  row.innerHTML=`<span class="agent-activity-status-icon">${iconMap[kind]||li('clock',13)}</span><span class="agent-activity-status-copy"><span class="agent-activity-status-label">${esc(label)}</span>${detail?`<span class="agent-activity-status-detail">${esc(detail)}</span>`:''}</span><span class="agent-activity-status-time">${esc(_activityClockLabel(ts))}</span>`;
  return row;
}
function _appendActivityEvent(group, event){
  if(!group)return null;
  const body=group.querySelector('.tool-call-group-body');
  if(!body)return null;
  const eventId=event&&event.id;
  let row=eventId?body.querySelector(`.agent-activity-status[data-activity-event-id="${CSS.escape(eventId)}"]`):null;
  const next=_activityStatusNode(event||{});
  if(row){row.replaceWith(next);row=next;}
  else{body.appendChild(next);row=next;}
  _activityMarkObserved(group,event&&event.ts);
  return row;
}
function _ensureLiveActivityBaseline(group){
  if(!group||group.getAttribute('data-live-tool-call-group')!=='1')return;
  const started=_activityElapsedStartedAt(group)||_activityNowSeconds();
  if(!group.getAttribute('data-turn-started-at')) group.setAttribute('data-turn-started-at',String(started));
  if(!group.getAttribute('data-last-activity-at')) group.setAttribute('data-last-activity-at',String(started));
  _appendActivityEvent(group,{id:'run-started',kind:'run',label:'Run started',detail:'Observable activity will appear here as the agent works.',status:'done',ts:started});
  const modelLabel=(S.session&&S.session.model)?getModelLabel(S.session.model):'';
  if(modelLabel)_appendActivityEvent(group,{id:'run-model',kind:'model',label:`Model: ${modelLabel}`,detail:S.activeProfile&&S.activeProfile!=='default'?`Profile: ${S.activeProfile}`:'',status:'done',ts:started});
}
function _setActivityElapsedStartedAt(group){
  if(!group||group.getAttribute('data-live-tool-call-group')!=='1')return;
  const started=_activityElapsedStartedAt(group);
  if(started)group.setAttribute('data-turn-started-at',String(started));
}
function _updateActiveActivityElapsedTimer(){
  const group=_activityElapsedTimerGroup;
  if(!group||!group.isConnected||group.getAttribute('data-live-tool-call-group')!=='1'||group.getAttribute('data-live-activity-current')!=='1'){
    _clearActivityElapsedTimer();
    return;
  }
  const durationEl=group.querySelector('.tool-call-group-duration');
  const label=_activityElapsedLabel(group);
  if(label){
    group.setAttribute('data-active-turn-elapsed',label);
  }else{
    group.removeAttribute('data-active-turn-elapsed');
  }
  if(durationEl){
    const activeText=label?`Working for ${label}`:'';
    const progressText=_activityLiveProgressLabel(group);
    durationEl.textContent=[progressText, activeText].filter(Boolean).join(' · ');
    durationEl.style.display=durationEl.textContent?'':'none';
  }
}
function _startActivityElapsedTimer(group){
  if(!group||group.getAttribute('data-live-tool-call-group')!=='1')return;
  _setActivityElapsedStartedAt(group);
  if(_activityElapsedTimerGroup&&_activityElapsedTimerGroup!==group)_clearActivityElapsedTimer();
  _activityElapsedTimerGroup=group;
  _updateActiveActivityElapsedTimer();
  if(!_activityElapsedTimer)_activityElapsedTimer=setInterval(_updateActiveActivityElapsedTimer,1000);
}
function _clearActivityElapsedTimer(){
  if(_activityElapsedTimer){
    clearInterval(_activityElapsedTimer);
    _activityElapsedTimer=null;
  }
  if(_activityElapsedTimerGroup&&_activityElapsedTimerGroup.isConnected){
    _activityElapsedTimerGroup.removeAttribute('data-active-turn-elapsed');
    const durationEl=_activityElapsedTimerGroup.querySelector('.tool-call-group-duration');
    if(durationEl){durationEl.textContent='';durationEl.style.display='none';}
  }
  _activityElapsedTimerGroup=null;
}

const _MOBILE_CONFIG_BASE_LABEL='Workspace, model, reasoning, and context settings';

function _setCtxCompressButton(btn,text){
  if(!btn)return;
  if(text){
    btn.style.display='';
    btn.textContent=text;
    btn.onclick=function(e){
      if(e)e.stopPropagation();
      const ta=$('msg');
      if(ta){ta.value='/compress ';ta.focus();autoResize();}
    };
  }else{
    btn.style.display='none';
    btn.textContent='';
    btn.onclick=null;
  }
}

function _syncMobileCtxDisplay(state){
  const mobileConfigBtn=$('composerMobileConfigBtn');
  const row=$('composerMobileContextAction');
  const usageLine=$('composerMobileContextUsage');
  const tokensLine=$('composerMobileContextTokens');
  const thresholdLine=$('composerMobileContextThreshold');
  const costLine=$('composerMobileContextCost');
  const compressBtn=$('composerMobileCtxCompressBtn');
  if(!state||!state.visible){
    if(row)row.style.display='none';
    if(mobileConfigBtn){
      mobileConfigBtn.setAttribute('aria-label',_MOBILE_CONFIG_BASE_LABEL);
      mobileConfigBtn.setAttribute('title',_MOBILE_CONFIG_BASE_LABEL);
    }
    _setCtxCompressButton(compressBtn,'');
    // Reset context ring to 0% to clear any stale values from previous sessions
    var arc = document.getElementById('ctx-arc');
    var num = document.getElementById('ctx-num');
    if (arc && num) {
      var circumference = 87.96;
      arc.setAttribute('stroke-dashoffset', circumference);
      num.textContent = '0';
      arc.setAttribute('stroke', '#22c55e');
    }
    return;
  }
  (function updateCtxRing(pct) {
    var arc = document.getElementById('ctx-arc');
    var num = document.getElementById('ctx-num');
    if (!arc || !num) return;
    var offset = 87.96 * (1 - Math.min(pct, 100) / 100);
    arc.setAttribute('stroke-dashoffset', offset);
    num.textContent = Math.round(pct);
    arc.setAttribute('stroke',
      pct <= 50 ? '#22c55e' : pct <= 85 ? '#f97316' : '#ef4444'
    );
  })(state.pct);
  if(mobileConfigBtn){
    mobileConfigBtn.setAttribute('aria-label',`${_MOBILE_CONFIG_BASE_LABEL}; ${state.label}`);
    mobileConfigBtn.setAttribute('title',`${_MOBILE_CONFIG_BASE_LABEL} \u00b7 ${state.label}`);
  }
  if(row){
    row.style.display='';
    row.setAttribute('aria-label',state.label);
    row.classList.toggle('ctx-mid',state.pct>50&&state.pct<=75);
    row.classList.toggle('ctx-high',state.pct>75);
  }
  if(usageLine)usageLine.textContent=state.usageText||'';
  if(tokensLine)tokensLine.textContent=state.tokensText||'';
  if(thresholdLine){
    if(state.thresholdText){
      thresholdLine.style.display='';
      thresholdLine.textContent=state.thresholdText;
    }else{
      thresholdLine.style.display='none';
      thresholdLine.textContent='';
    }
  }
  if(costLine){
    if(state.costText){
      costLine.style.display='';
      costLine.textContent=state.costText;
    }else{
      costLine.style.display='none';
      costLine.textContent='';
    }
  }
  _setCtxCompressButton(compressBtn,state.compressText||'');
}

function _mergeUsageForCtxIndicator(latest, fallback){
  const latestObj=(latest&&typeof latest==='object')?latest:{};
  const fallbackObj=(fallback&&typeof fallback==='object')?fallback:{};
  const merged={...latestObj};
  for(const field of [
    'input_tokens','output_tokens','estimated_cost',
    'cache_read_tokens','cache_write_tokens','cache_hit_percent',
    'turn_cache_hit_percent','duration_seconds','tps','gateway_routing',
  ]){
    if(merged[field]==null&&fallbackObj[field]!=null){
      merged[field]=fallbackObj[field];
    }
  }
  if(!(Number(latestObj.context_length)>0)&&Number(fallbackObj.context_length)>0){
    merged.context_length=fallbackObj.context_length;
  }
  for(const field of ['threshold_tokens','last_prompt_tokens']){
    if(latestObj[field]==null&&fallbackObj[field]!=null){
      merged[field]=fallbackObj[field];
    }
  }
  return merged;
}

// Context usage indicator in composer footer
function _syncCtxIndicator(usage){
  const wrap=$('ctxIndicatorWrap');
  const el=$('ctxIndicator');
  if(!el)return;
  // #1436: Use last_prompt_tokens only — NEVER fall back to cumulative
  // input_tokens for the "context window % used" calculation.  input_tokens
  // is summed across all turns, so dividing it by the context window gives a
  // nonsense percentage (often >100%) on long sessions.  When we have no
  // last-prompt data we render "·" + "tokens used" via the !hasPromptTok
  // branch below — honest "no data" instead of misleading "890% used".
  const promptTok=usage.last_prompt_tokens||0;
  const totalTok=(usage.input_tokens||0)+(usage.output_tokens||0);
  const cacheReadTok=usage.cache_read_tokens||0;
  const cacheWriteTok=usage.cache_write_tokens||0;
  // Default context window to 128K when not provided by backend
  const DEFAULT_CTX=128*1024;
  const ctxWindow=usage.context_length||DEFAULT_CTX;
  const cost=usage.estimated_cost;
  // Show indicator whenever we have any usage data (tokens or cost)
  if(!promptTok&&!totalTok&&!cost&&!cacheReadTok&&!cacheWriteTok){
    if(wrap) wrap.style.display='none';
    _syncMobileCtxDisplay({visible:false});
    return;
  }
  if(wrap) wrap.style.display='';
  const hasPromptTok=!!promptTok;
  const rawPct=hasPromptTok?Math.round((promptTok/ctxWindow)*100):0;
  const pct=Math.min(100,rawPct);
  const overflowed=rawPct>100;
  const ring=$('ctxRingValue');
  const center=$('ctxPercent');
  const usageLine=$('ctxTooltipUsage');
  const tokensLine=$('ctxTooltipTokens');
  const thresholdLine=$('ctxTooltipThreshold');
  const costLine=$('ctxTooltipCost');
  if(ring){
    const circumference=61.261056745;
    ring.style.strokeDasharray=String(circumference);
    ring.style.strokeDashoffset=String(circumference*(1-pct/100));
  }
  if(center) center.textContent=hasPromptTok?String(pct):'\u00b7';
  const hasExplicitCtx=!!usage.context_length;
  el.classList.toggle('ctx-mid',pct>50&&pct<=75);
  el.classList.toggle('ctx-high',pct>75);
  // ── Compress affordance (#524) ──
  // Show a hint in the tooltip when context usage is high so users
  // discover /compress without having to know the slash command.
  const compressWrap=$('ctxTooltipCompress');
  const compressBtn=$('ctxCompressBtn');
  const compressText=pct>=75?t('ctx_compress_action'):(pct>=50?t('ctx_compress_hint'):'');
  if(compressWrap) compressWrap.style.display=compressText?'':'none';
  _setCtxCompressButton(compressBtn,compressText);
  const cacheHitPct=usage.cache_hit_percent;
  const cacheText=cacheHitPct!=null?t('usage_cache_hit_detail',cacheHitPct,_fmtTokens(cacheReadTok),_fmtTokens(cacheWriteTok)):'';
  let label=hasPromptTok?`Context window ${pct}% used`:`${_fmtTokens(totalTok)} tokens used`;
  if(!hasExplicitCtx&&hasPromptTok) label+=' (est. 128K)';
  if(cost) label+=` \u00b7 $${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
  if(cacheText) label+=` \u00b7 ${cacheText}`;
  el.setAttribute('aria-label',label);
  const usageText=hasPromptTok?(overflowed?`${rawPct}% used (context exceeded)`:`${pct}% used (${100-pct}% left)`):`${_fmtTokens(totalTok)} tokens used`;
  const tokensText=hasPromptTok?`${_fmtTokens(promptTok)} / ${_fmtTokens(ctxWindow)} tokens used`:`In: ${_fmtTokens(usage.input_tokens||0)} \u00b7 Out: ${_fmtTokens(usage.output_tokens||0)}`;
  if(usageLine) usageLine.textContent=usageText;
  if(tokensLine) tokensLine.textContent=tokensText;
  const threshold=usage.threshold_tokens||0;
  let thresholdText='';
  if(thresholdLine){
    if(threshold&&ctxWindow){
      thresholdText=`Auto-compress at ${_fmtTokens(threshold)} (${Math.round(threshold/ctxWindow*100)}%)`;
      thresholdLine.style.display='';
      thresholdLine.textContent=thresholdText;
    }else{
      thresholdLine.style.display='none';
      thresholdLine.textContent='';
    }
  }
  let costText='';
  if(costLine){
    if(cost){
      costText=`Estimated cost: $${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
      if(cacheText) costText+=` \u00b7 ${cacheText}`;
      costLine.style.display='';
      costLine.textContent=costText;
    }else if(cacheText){
      costText=cacheText;
      costLine.style.display='';
      costLine.textContent=costText;
    }else{
      costLine.style.display='none';
      costLine.textContent='';
    }
  }
  _syncMobileCtxDisplay({
    visible:true,
    hasPromptTok,
    pct,
    label,
    usageText,
    tokensText,
    thresholdText,
    costText,
    compressText
  });
}

// ── Touch support: toggle context tooltip on tap (#524) ──
// On mobile, hover doesn't work — allow tap on the context ring button
// to toggle the tooltip visibility so the compress affordance is reachable.
document.addEventListener('DOMContentLoaded',function(){
  const wrap=document.getElementById('ctxIndicatorWrap');
  const tooltip=document.getElementById('ctxTooltip');
  if(!wrap||!tooltip)return;
  const btn=document.getElementById('ctxIndicator');
  if(!btn)return;
  btn.addEventListener('click',function(e){
    e.stopPropagation();
    const isOpen=tooltip.classList.contains('ctx-tooltip-active');
    tooltip.classList.toggle('ctx-tooltip-active',!isOpen);
    tooltip.setAttribute('aria-hidden',String(isOpen));
  });
  // Close on outside tap
  document.addEventListener('click',function(){
    tooltip.classList.remove('ctx-tooltip-active');
    tooltip.setAttribute('aria-hidden','true');
  },{passive:true});
  // Prevent tooltip click from closing itself
  tooltip.addEventListener('click',function(e){e.stopPropagation();});
});

function _setMessageScrollToBottom(){
  const el=$('messages');
  if(!el) return;
  _programmaticScroll=true;
  el.scrollTop=el.scrollHeight;
  _lastScrollTop=el.scrollTop;
  _nearBottomCount=2;
  _scrollPinned=true;
  requestAnimationFrame(()=>{
    // Retry the bottom write on the next layout frame so a DOM rebuild that
    // grows the transcript after the first write doesn't strand a pinned
    // conversation mid-scroll (#3319). But by this frame the user may have
    // scrolled up — under the sticky-unpin model (#3343) _messageUserUnpinned
    // is the authoritative "user scrolled away" signal, so DON'T snap them back
    // or re-pin if so; only release the programmatic-scroll latch.
    if(_messageUserUnpinned || !_scrollPinned || _recentNonMessageScrollIntent()){
      requestAnimationFrame(()=>{ setTimeout(()=>{_programmaticScroll=false;},0); });
      return;
    }
    el.scrollTop=el.scrollHeight;
    _lastScrollTop=el.scrollTop;
    _nearBottomCount=2;
    _scrollPinned=true;
    requestAnimationFrame(()=>{ setTimeout(()=>{_programmaticScroll=false;},0); });
  });
}
function _isMessagePaneNearBottom(threshold=250){
  const el=$('messages');
  if(!el) return false;
  return el.scrollHeight-el.scrollTop-el.clientHeight<=threshold;
}
function _messageBottomDistance(){
  const el=$('messages');
  if(!el) return 0;
  return el.scrollHeight-el.scrollTop-el.clientHeight;
}
function _shouldFollowMessagesOnDomReplace(){
  // Final stream settlement replaces the live DOM with persisted messages. Keep
  // following only for users who are still pinned or effectively at the tail.
  // A broad near-bottom window causes long answers/mobile readers who scroll up
  // a little to read mid-stream to get snapped back to the bottom on completion.
  return !_messageUserUnpinned && (_scrollPinned || _isMessagePaneNearBottom(120));
}
function _settleMessageScrollToBottom(force){
  // Markdown post-processing (Prism, tables, Mermaid/KaTeX/PDF placeholders)
  // can grow the transcript after the first scroll write. Re-apply the bottom
  // position across a few frames while pinned so late layout does not leave the
  // viewport a few lines above the real end. User scroll increments
  // _bottomSettleToken and cancels the delayed passes.
  const token=++_bottomSettleToken;
  const passes=[0,16,80,180];
  passes.forEach(delay=>setTimeout(()=>{
    if(token!==_bottomSettleToken) return;
    if(!force && (!_scrollPinned||_messageUserUnpinned||_recentNonMessageScrollIntent())) return;
    _setMessageScrollToBottom();
  },delay));
  requestAnimationFrame(()=>{
    if(token!==_bottomSettleToken) return;
    if(force || (_scrollPinned&&!_messageUserUnpinned&&!_recentNonMessageScrollIntent())) _setMessageScrollToBottom();
    requestAnimationFrame(()=>{
      if(token!==_bottomSettleToken) return;
      if(force || (_scrollPinned&&!_messageUserUnpinned&&!_recentNonMessageScrollIntent())) _setMessageScrollToBottom();
    });
  });
}
function scrollIfPinned(){
  if(_messageUserUnpinned) return;
  if(!_scrollPinned) return;
  if(_recentNonMessageScrollIntent()) return;
  if(_messageBottomDistance()>500) _setMessageScrollToBottom();
  _settleMessageScrollToBottom(false);
}
function scrollToBottom(){
  _scrollPinned=true;
  _messageUserUnpinned=false;
  // Write the first bottom position synchronously. A final renderMessages()
  // rebuild can queue a native scroll event from the temporary scrollTop=0
  // layout state; if we only schedule delayed settles, that event can cancel
  // them before the viewport ever reaches the bottom.
  _setMessageScrollToBottom();
  _settleMessageScrollToBottom(true);
  const btn=$('scrollToBottomBtn');
  if(btn) btn.style.display='none';
  if(typeof _updateSessionStartJumpButton==='function') _updateSessionStartJumpButton();
}

function _fmtOllamaLabel(mid){
  const [namePart, ...variantParts] = mid.split(':');
  const variant = variantParts.join(':');
  const _fmt = (s) => {
    const tokens = s.replace(/[-_]/g, ' ').split(' ');
    return tokens.map(t => {
      const alphaOnly = t.replace(/\./g, '');
      if (t.length <= 3 && /^[a-zA-Z.]+$/.test(t)) return t.toUpperCase();
      if (/^\d/.test(alphaOnly)) return t.toUpperCase();
      return t.charAt(0).toUpperCase() + t.slice(1);
    }).join(' ');
  };
  let label = _fmt(namePart);
  if (variant) label += ' (' + _fmt(variant) + ')';
  return label;
}

function getModelLabel(modelId){
  if(!modelId) return 'Unknown';
  const rawId=String(modelId||'');
  // Preserve custom gateway model IDs exactly as configured.
  // Examples:
  //   @custom:ai_gateway:Qwen3.6-35B-A3B -> Qwen3.6-35B-A3B
  //   @custom:qwen397b-64k               -> qwen397b-64k
  if(rawId.startsWith('@custom:')){
    const rest=rawId.slice('@custom:'.length);
    if(rest.includes(':')) return rest.slice(rest.lastIndexOf(':')+1)||rawId;
    if(rest.includes('/')) return rest.slice(rest.indexOf('/')+1)||rawId;
    return rest||rawId;
  }
  // Check dynamic labels first, then fall back to splitting the ID
  if(_dynamicModelLabels[modelId]) return _dynamicModelLabels[modelId];
  // Static fallback for common models
  const STATIC_LABELS={'openai/gpt-5.4-mini':'GPT-5.4 Mini','openai/gpt-4o':'GPT-4o','openai/o3':'o3','openai/o4-mini':'o4-mini','anthropic/claude-sonnet-4.6':'Sonnet 4.6','anthropic/claude-sonnet-4-5':'Sonnet 4.5','anthropic/claude-haiku-3-5':'Haiku 3.5','google/gemini-3.1-pro-preview':'Gemini 3.1 Pro','google/gemini-3-flash-preview':'Gemini 3 Flash','google/gemini-3.1-flash-lite-preview':'Gemini 3.1 Flash Lite','google/gemini-2.5-pro':'Gemini 2.5 Pro','google/gemini-2.5-flash':'Gemini 2.5 Flash','deepseek/deepseek-v4-flash':'DeepSeek V4 Flash','deepseek/deepseek-v4-pro':'DeepSeek V4 Pro','deepseek/deepseek-chat-v3-0324':'DeepSeek V3 (legacy)','meta-llama/llama-4-scout':'Llama 4 Scout'};
  if(STATIC_LABELS[modelId]) return STATIC_LABELS[modelId];
  // Safe Ollama-tag fallback: strip only the first slash-segment (provider
  // prefix) so multi-slash IDs preserve their vendor hierarchy (#3360).
  // URI-scheme ids (e.g. `gpt://${FOLDER}/deepseek-v4-flash/latest`, provider
  // `yandex:gpt`) must NOT be first-segment-stripped — `indexOf('/')` would
  // land inside the `://` and leave `/${FOLDER}/...` path junk (#3429). For a
  // `scheme://authority/path...` id, drop the scheme AND the authority, then
  // pick the model name from the PATH segments only. A version/channel tail
  // (`latest`/`stable`/numeric) is skipped only when a real model segment
  // precedes it — never promoting the authority or a container folder (#3429).
  let _last;
  const _uriMatch = /^[a-z][a-z0-9+.-]*:\/\/(.+)$/i.exec(modelId);
  if (_uriMatch) {
    const _all = _uriMatch[1].split('/').filter(Boolean);
    // _all[0] is the authority (folder/host); the model lives in the path tail.
    const _path = _all.slice(1);
    // A pure version/channel tail: named channels, or a bare version number
    // (`v4`, `1.2`, `20231231`) — NOT a mixed model name that merely starts
    // with a digit (`2026-model`, `4o-mini`), which must be kept as the label.
    const _isVersionTail = (s) => /^(latest|stable|current|default|v\d[\d.]*|\d[\d.]*)$/i.test(s);
    const _isPlaceholder = (s) => /\$\{[^}]*\}/.test(s);
    // Walk path segments right-to-left; the model name is the LAST segment that
    // is neither a version/channel tail (`latest`, `v4`, `1.2`) nor a `${...}`
    // env-var placeholder. Fall back to the last non-placeholder segment, then
    // the literal last segment. Never returns the authority (`_all[0]`).
    let _pick = '';
    let _lastUsable = '';
    for (let _i = _path.length - 1; _i >= 0; _i--) {
      const _seg = _path[_i];
      if (_isPlaceholder(_seg)) continue;
      if (!_lastUsable) _lastUsable = _seg;
      if (!_isVersionTail(_seg)) { _pick = _seg; break; }
    }
    // Fallbacks: the chosen non-version segment, else the last non-placeholder
    // path segment. NEVER the authority and NEVER a `${...}` placeholder — for
    // a degenerate id (`gpt://folder123`, `gpt://folder123/${MODEL}`) fall all
    // the way back to the raw id rather than leak the folder/host or env var.
    const _lastPath = _path[_path.length - 1] || '';
    _last = _pick || _lastUsable || (_lastPath && !_isPlaceholder(_lastPath) ? _lastPath : '') || modelId;
  } else {
    _last = modelId.includes('/') ? (modelId.slice(modelId.indexOf('/')+1) || modelId) : modelId;
  }
  // Strip @provider: prefix if present (e.g. @ollama-cloud:kimi-k2.6)
  if (_last.startsWith('@') && _last.includes(':')) _last = _last.split(':').slice(1).join(':');
  const looksLikeOllamaTag = /^[a-z0-9][\w.-]*:[\w.-]+$/i.test(_last);
  const atProvider=(rawId.startsWith('@')&&rawId.includes(':'))
    ? rawId.slice(1,rawId.indexOf(':')).toLowerCase()
    : '';
  const allowOllamaFormat=!atProvider||atProvider.startsWith('ollama');
  // Narrow: only apply Ollama formatter to IDs with explicit @ollama prefix or colon-tag format.
  // Avoids reformatting bare provider model IDs like claude-sonnet-4-6 or gpt-4o.
  const looksLikeBareOllamaId = modelId.startsWith('@ollama') || looksLikeOllamaTag;
  const ollamaLabel = _fmtOllamaLabel(_last);
  if (allowOllamaFormat && (modelId.startsWith('ollama/') || modelId.startsWith('@ollama') || looksLikeOllamaTag || looksLikeBareOllamaId) && ollamaLabel !== _last) {
    return ollamaLabel;
  }
  return _last || 'Unknown';
}

function _gatewayProviderName(provider){
  const text=String(provider||'').trim();
  if(!text)return'';
  return text.replace(/^custom:/,'').replace(/[-_]/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
}
function _gatewayRoutingLabel(routing){
  if(!routing)return'';
  const provider=_gatewayProviderName(routing.used_provider||routing.provider);
  return provider?`via ${provider}`:'';
}
function _formatGatewayModelLabel(modelId,labelText,routing){
  if(!routing)return'';
  const usedModel=String(routing.used_model||'').trim();
  const base=usedModel?getModelLabel(usedModel):(labelText||getModelLabel(modelId));
  const via=_gatewayRoutingLabel(routing);
  return via?`${base} ${via}`:base;
}
function _gatewayRoutingFailoverText(routing){
  if(!routing||!routing.has_failover)return'';
  const attempts=Array.isArray(routing.routing)?routing.routing:[];
  const providers=attempts.map(a=>_gatewayProviderName(a&&a.provider)).filter(Boolean);
  const unique=[];providers.forEach(p=>{if(!unique.includes(p))unique.push(p);});
  if(unique.length>=2)return`Failover: ${unique[0]} → ${unique[unique.length-1]}`;
  const from=_gatewayProviderName(routing.requested_provider);
  const to=_gatewayProviderName(routing.used_provider);
  if(from&&to&&from!==to)return`Failover: ${from} → ${to}`;
  return'Gateway failover detected';
}
function _gatewayModelWarningText(routing){
  if(!routing||!routing.model_changed)return'';
  const requested=getModelLabel(routing.requested_model||'requested model');
  const used=getModelLabel(routing.used_model||'served model');
  return`Model switched: ${requested} → ${used}`;
}
function _latestGatewayRoutingForSession(session){
  if(!session)return null;
  if(session.gateway_routing)return session.gateway_routing;
  const history=Array.isArray(session.gateway_routing_history)?session.gateway_routing_history:[];
  return history.length?history[history.length-1]:null;
}

function _stripXmlToolCallsDisplay(s){
  // Strip <function_calls>...</function_calls> blocks emitted by DeepSeek and
  // similar models in their raw response text.  These are processed separately
  // as tool calls; leaving them in the content causes them to render visibly
  // in the settled chat bubble.  (#702)
  // Also handles DSML-prefixed variants from DeepSeek/Bedrock, including
  // spacing variants like "<｜DSML |function_calls" and truncated prefixes.
  if(!s) return s;
  const lo=String(s).toLowerCase();
  if(lo.indexOf('function_calls')===-1 && lo.indexOf('dsml')===-1) return s;
  // Support both plain <function_calls> and DSML-prefixed variants.
  s=s.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>[\s\S]*?<\/(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>/gi,'');
  // Also remove truncated opening tags (missing closing ">" at stream tail).
  s=s.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls(?:>|$)[\s\S]*$/i,'');
  // Remove malformed DSML tag fragments like "<｜DSML |" that can leak in tokens.
  s=s.replace(/<\s*｜\s*DSML\s*[｜|]\s*/gi,'');
  return s.trim();
}

function _sanitizeThinkingDisplayText(text){
  const stripped=_stripXmlToolCallsDisplay(String(text||''));
  return stripped.trim();
}

function _stripVisibleAssistantEchoFromThinking(thinkingText, visibleText){
  let out=String(thinkingText||'');
  const visible=String(visibleText||'');
  if(!out||!visible) return out.trim();
  visible.split(/\n{2,}/).map(s=>s.trim()).filter(s=>s.length>=20).forEach(snippet=>{
    out=out.split(snippet).join('');
  });
  return out.trim();
}

function renderMd(raw){
  let s=(raw||'').replace(/\r\n/g,'\n').replace(/\r/g,'\n');
  // ── Entity decode: must run FIRST so &gt; lines become > for the blockquote
  // pre-pass below. LLMs sometimes emit HTML-entity-encoded output; without this
  // a blockquote sent as "&gt; text" would never be recognised as a blockquote.
  s=s.replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').replace(/&quot;/g,'"').replace(/&#39;/g,"'");
  // ── Blockquote pre-pass (must run BEFORE every other markdown pass) ────────
  // Group consecutive >-prefixed lines, strip the > prefix from each line,
  // recursively render the stripped content with the full pipeline, and
  // replace the group with a stash token. This is the only way fenced code,
  // headings, hr, and ordered lists inside a blockquote can render correctly:
  // the per-line passes downstream don't know about > prefixes, and by the
  // time the blockquote handler used to run those passes had already mangled
  // the >-prefixed lines.
  //
  // Walks lines (instead of using a single regex) so >-prefixed lines that
  // sit inside a non-blockquote fenced block (e.g. a shell prompt in a
  // ```bash``` example) are not miscaptured as a blockquote.
  const _bq_stash=[];
  s=(function _applyBlockquotes(input){
    const lines=input.split('\n');
    const out=[];
    let inFence=false;     // inside a non-blockquote backtick fence
    let fenceLen=0;
    let bqStart=-1;
    const flush=(end)=>{
      if(bqStart<0) return;
      // Strip "> " prefix (and bare ">" → empty) from each line
      const stripped=lines.slice(bqStart,end).map(l=>l.replace(/^> ?/,'')).join('\n');
      // Recursive call: full pipeline on stripped content. Handles fenced
      // code, headings, hr, ordered/unordered lists, nested blockquotes
      // (>>) — anything that renderMd handles at the top level.
      const rendered=renderMd(stripped);
      _bq_stash.push('<blockquote>'+rendered+'</blockquote>');
      // Surround the token with blank lines so the paragraph splitter
      // isolates it as its own chunk (otherwise the token gets wrapped
      // in <p>...<br> with adjacent text, producing invalid HTML).
      out.push('');
      out.push('\x00Q'+(_bq_stash.length-1)+'\x00');
      out.push('');
      bqStart=-1;
    };
    for(let i=0;i<lines.length;i++){
      const line=lines[i];
      if(inFence){
        out.push(line);
        if(_isBacktickFenceClose(line,fenceLen)){inFence=false;fenceLen=0;}
        continue;
      }
      const fenceOpen=_matchBacktickFenceLine(line);
      if(fenceOpen){
        flush(i);
        out.push(line);
        inFence=true;
        fenceLen=fenceOpen.len;
        continue;
      }
      if(/^>/.test(line)){
        if(bqStart<0) bqStart=i;
      } else {
        flush(i);
        out.push(line);
      }
    }
    flush(lines.length);
    return out.join('\n');
  })(s);
  // ── MEDIA: token stash (must run first, before any other processing) ───────
  // Detect MEDIA:<path-or-url> tokens emitted by the agent (e.g. screenshots,
  // generated images) and replace them with inline <img> or download links.
  // Stashed so the path/URL is never processed as markdown.
  const media_stash=[];
  s=s.replace(/MEDIA:([^\s\)\]]+)/g,(_,raw_ref)=>{
    media_stash.push(raw_ref);
    return '\x00D'+(media_stash.length-1)+'\x00';
  });
  // ── End MEDIA stash ─────────────────────────────────────────────────────────
  // Pre-pass: decode HTML entities first so markdown processing works correctly.
  // This prevents double-escaping when LLM outputs entities like &lt; &gt; &amp;
  const decode=s=>s.replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').replace(/&quot;/g,'"').replace(/&#39;/g,"'");
  s=decode(s);
  // Pre-pass: convert safe inline HTML tags the model may emit into their
  // markdown equivalents so the pipeline can render them correctly.
  // Only runs OUTSIDE fenced code blocks and backtick spans (stash + restore).
  // Unsafe tags (anything not in the allowlist) are left as-is and will be
  // HTML-escaped by esc() when they reach an innerHTML assignment -- no XSS risk.
  // Fence stash: protect code blocks and backtick spans from all further processing.
  // Must run BEFORE math_stash so $..$ inside code spans is not extracted as math.
  // Split into fenced blocks (\x00P — kept stashed until after all markdown passes)
  // and inline backtick spans (\x00F — restored before bold/italic so **`code`** works).
  // Fenced blocks are converted to <pre><code> here so their content is HTML-escaped
  // and never exposed to list/heading/table regexes that could corrupt the layout.
  // Fixes #1154: diff/patch lines inside fenced blocks (e.g. + added, - removed)
  // were matching the unordered-list regex and injecting <ul>/<li> inside <pre>,
  // breaking </pre> closure and corrupting all subsequent message rendering.
  const _preBlock_stash=[];
  const fence_stash=[];
  // CommonMark §4.5: opening fence must start a line (with up to 3 spaces of indent)
  // and closing fence must start a line with the same backtick char and at least
  // as many backticks as the opener. Without line/fence-length anchoring, a literal
  // ``` inside a code block (e.g. a nested markdown example) terminates the outer
  // block at the wrong place, leaking content into the markdown stream where
  // bold/italic/inline-code passes corrupt it. Fixes #1438 and #1696.
  s=s.replace(/(^|\n)[ ]{0,3}(`{3,})([^\n`]*)\n(?:([\s\S]*?)\n)?[ ]{0,3}\2`*[ \t]*(?=\n|$)/g,(_,lead,_fence,info,code)=>{
    const langInfo=(info||'').trim();
    const langMatch=langInfo.match(/^(\w[\w+-]*)$/);
    const lang=langMatch?(langMatch[1]||'').trim().toLowerCase():'';
    code=code||'';
    const codeLines=code.split('\n');
    const firstCodeLine=codeLines.find(line=>line.trim())||'';
    const firstMermaidLine=codeLines.map(line=>line.trim()).find(line=>line&&!line.startsWith('%%'))||'';
    const looksLikeLineNumberedToolOutput=/^\s*\d+\|/.test(firstCodeLine);
    const looksLikeMermaidStart=firstMermaidLine==='---'||/^(graph|flowchart|sequenceDiagram|classDiagram|classDiagram-v2|stateDiagram|stateDiagram-v2|erDiagram|journey|gantt|pie|gitGraph|mindmap|timeline|quadrantChart|requirementDiagram|C4Context|C4Container|C4Component|C4Dynamic|c4Context|c4Container|c4Component|c4Dynamic|sankey-beta|block-beta|packet-beta|xychart-beta|kanban|architecture-beta)\b/.test(firstMermaidLine);
    if(lang==='mermaid'&&!looksLikeLineNumberedToolOutput&&looksLikeMermaidStart){
      const id='mermaid-'+Math.random().toString(36).slice(2,10);
      _preBlock_stash.push(`<div class="mermaid-block" data-mermaid-id="${id}">${esc(code.trim())}</div>`);
    } else {
      const h=lang?`<div class="pre-header">${esc(lang)}</div>`:'';
      const langAttr=lang?` class="language-${esc(lang)}"`:'';
      // For diff/patch blocks, wrap each line in a colored span
      if(lang==='diff'||lang==='patch'){
        const colored=esc(code.replace(/\n$/,'')).split('\n').map(line=>{
          if(line.startsWith('@@')) return `<span class="diff-line diff-hunk">${line}</span>`;
          if(line.startsWith('+')) return `<span class="diff-line diff-plus">${line}</span>`;
          if(line.startsWith('-')) return `<span class="diff-line diff-minus">${line}</span>`;
          return `<span class="diff-line">${line}</span>`;
        }).join('\n');
        _preBlock_stash.push(`${h}<pre class="diff-block"><code${langAttr}>${colored}</code></pre>`);
      // For JSON/YAML blocks, add tree-view placeholder with raw data
      } else if(lang==='json'||lang==='yaml'){
        const rawCode=esc(code.replace(/\n$/,''));
        // Encode newlines as &#10; to prevent HTML attribute normalization
        // (browsers collapse \n to spaces inside attribute values).
        const rawAttr=rawCode.replace(/"/g,'&quot;').replace(/\n/g,'&#10;');
        const blockId='tree-'+Math.random().toString(36).slice(2,10);
        _preBlock_stash.push(`<div class="code-tree-wrap" data-raw="${rawAttr}" data-lang="${lang}" id="${blockId}">${h}<pre class="tree-raw-view"><code${langAttr}>${rawCode}</code></pre></div>`);
      // CSV blocks → render as styled table
      } else if(lang==='csv'){
        const rows=code.replace(/\n$/,'').split('\n').filter(r=>r.trim());
        if(rows.length>=2){
          const headers=rows[0].split(',').map(c=>c.trim());
          const body=rows.slice(1).map(r=>'<tr>'+r.split(',').map(c=>`<td>${esc(c.trim())}</td>`).join('')+'</tr>').join('');
          _preBlock_stash.push(`${h}<div class="csv-table-wrap"><table class="csv-table"><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>`);
        } else {
          _preBlock_stash.push(`${h}<pre><code${langAttr}>${esc(code.replace(/\n$/,''))}</code></pre>`);
        }
      } else {
        _preBlock_stash.push(`${h}<pre><code${langAttr}>${esc(code.replace(/\n$/,''))}</code></pre>`);
      }
    }
    return lead+'\x00P'+(_preBlock_stash.length-1)+'\x00';
  });
  s=s.replace(/`([^`\n]+)`/g,(_,c)=>{fence_stash.push('<code>'+esc(c)+'</code>');return '\x00F'+(fence_stash.length-1)+'\x00';});
  // Math stash: protect $$..$$ and $..$ from markdown processing
  // Runs AFTER fence_stash so backtick code spans protect their dollar-sign contents
  const math_stash=[];
  // Display math: $$...$$ and \[...\] (must come before inline to avoid mis-parsing)
  s=s.replace(/\$\$([\s\S]+?)\$\$/g,(_,m)=>{math_stash.push({type:'display',src:m});return '\x00M'+(math_stash.length-1)+'\x00';});
  // Match a single literal backslash before the display delimiter (the common LLM form).
  s=s.replace(/\\\[([\s\S]+?)\\\]/g,(_,m)=>{math_stash.push({type:'display',src:m});return '\x00M'+(math_stash.length-1)+'\x00';});
  // Inline math: $...$ — require non-space/non-digit at opening boundary to avoid
  // false positives on currency like "$1,000 xuống ~$95" or "costs $5 and $10".
  // Aligns with smd's se() guard which also rejects $ followed by digits.
  s=s.replace(/\$([^\s$\d\n][^$\n]*?[^\s$\n]|[^\s\d])\$/g,(_,m)=>{if(m.includes(' | '))return '\$'+m+'\$';math_stash.push({type:'inline',src:m});return '\x00M'+(math_stash.length-1)+'\x00';});
  // Also stash \(...\) LaTeX delimiters.
  // Match a single literal backslash before the delimiter (the common LLM form).
  s=s.replace(/\\\((.+?)\\\)/g,(_,m)=>{math_stash.push({type:'inline',src:m});return '\x00M'+(math_stash.length-1)+'\x00';});
  // Safe tag → markdown equivalent (these produce the same output as **text** etc.)
  // Stash raw <pre> blocks so the inline <code> rewrite below does not run
  // inside them. Running that rewrite in <pre> content can introduce stray
  // backticks for multiline code and break subsequent code-box rendering.
  const rawPreStash=[];
  s=s.replace(/(<pre\b[^>]*>[\s\S]*?<\/pre>)/gi,m=>{rawPreStash.push(m);return `\x00R${rawPreStash.length-1}\x00`;});
  // Bare file:// artifact links → media. Some gateway/tool surfaces emit bare
  // file:// links for local artifacts instead of MEDIA: tokens; browser clients
  // cannot open the server filesystem directly, so route them through /api/media.
  // Runs AFTER fenced-block (\x00P), inline-code (\x00F), AND raw-<pre> (\x00R)
  // stashing so a file:// inside any code/preformatted region stays literal text
  // (#3219/#3234). Only bare URLs (line-start or whitespace-delimited) match, so
  // normal [label](file://...) markdown anchors keep the link path below.
  s=s.replace(/(^|\s)(file:\/\/[^\s<>"')\]]+)/g,(_,lead,raw_ref)=>{
    media_stash.push(raw_ref);
    return lead+'\x00D'+(media_stash.length-1)+'\x00';
  });
  s=s.replace(/<strong>([\s\S]*?)<\/strong>/gi,(_,t)=>'**'+t+'**');
  s=s.replace(/<b>([\s\S]*?)<\/b>/gi,(_,t)=>'**'+t+'**');
  s=s.replace(/<em>([\s\S]*?)<\/em>/gi,(_,t)=>'*'+t+'*');
  s=s.replace(/<i>([\s\S]*?)<\/i>/gi,(_,t)=>'*'+t+'*');
  s=s.replace(/<code>([^<]*?)<\/code>/gi,(_,t)=>'`'+t+'`');
  s=s.replace(/<br\s*\/?>/gi,'\n');
  // ── Glued-bold-heading lift (issue #1446) ────────────────────────────────
  // LLMs in thinking/reasoning mode frequently emit a "section header" glued
  // to the end of the previous paragraph with no whitespace, like:
  //
  //   Para 1 text.**Heading to Para 2**
  //
  //   Para 2 text.**Heading to Para 3**
  //
  // CommonMark renders that correctly as paragraph-end inline bold, but the
  // visual effect is a run-on label rather than a section break. Lift the
  // glued bold into its own paragraph when it follows a sentence terminator
  // and is followed by a blank line.
  //
  // Constraints (avoid false positives):
  //   - Trigger only on a sentence terminator (.!?) IMMEDIATELY before `**`
  //     (no space) — that pattern is almost always a glued heading, not
  //     intentional emphasis.
  //   - Inner text length ≤ 80 chars — long bold runs are usually emphasis
  //     prose, not headings.
  //   - Trailing `\n\n` required — preserves mid-paragraph emphasis like
  //     "this is **important**." untouched.
  //   - Inner text must not contain newlines or `*` (single-line bold only).
  //   - Runs after fenced code, math, and raw <pre> are stashed, so code
  //     content is protected (see pipeline notes).
  s=s.replace(/([.!?])\*\*([^*\n]{1,80})\*\*\n\n/g,'$1\n\n**$2**\n\n');
  // Inline backtick spans: restore <code> tags produced in the stash callback above.
  // Must happen BEFORE bold/italic so **`code`** → <strong><code>code</code></strong>.
  s=s.replace(/\x00F(\d+)\x00/g,(_,i)=>fence_stash[+i]);
  // inlineMd: process bold/italic/code/links within a single line of text.
  // Used inside list items and blockquotes where the text may already contain
  // HTML from the pre-pass → bold pipeline, so we cannot call esc() directly.
  function inlineMd(t){
    // Stash backtick code spans first so bold/italic never esc() their content
    const _code_stash=[];
    t=t.replace(/`([^`\n]+)`/g,(_,x)=>{_code_stash.push(`<code>${esc(x)}</code>`);return `\x00C${_code_stash.length-1}\x00`;});
    t=t.replace(/\*\*\*(.+?)\*\*\*/g,(_,x)=>`<strong><em>${esc(x)}</em></strong>`);
    t=t.replace(/\*\*(.+?)\*\*/g,(_,x)=>`<strong>${esc(x)}</strong>`);
    t=t.replace(/\*([^*\n]+)\*/g,(_,x)=>`<em>${esc(x)}</em>`);
    // Strikethrough: ~~text~~ → <del>text</del>
    t=t.replace(/~~(.+?)~~/g,(_,x)=>`<del>${esc(x)}</del>`);
    // #487: Image pass — runs while code stash is active so ![x](url) inside
    // backticks stays protected as a \x00C token and is never rendered as <img>.
    // Must run before _code_stash restore and before _link_stash so the image
    // is not consumed by the [label](url) link regex.
    t=t.replace(/!\[([^\]]*)\]\((https?:\/\/[^\)]+)\)/g,(_,alt,url)=>`<img src="${url.replace(/"/g,'%22')}" alt="${esc(alt)}" class="msg-media-img" loading="lazy">`);
    // Stash rendered <img> tags so autolink never matches URLs inside src=
    const _img_stash=[];
    t=t.replace(/(<img\b[^>]*>)/g,m=>{_img_stash.push(m);return `\x00G${_img_stash.length-1}\x00`;});
    t=t.replace(/\x00C(\d+)\x00/g,(_,i)=>_code_stash[+i]);
    // Stash [label](url) links before autolink so the URL in href= is not re-linked
    const _link_stash=[];
    t=t.replace(/\[([^\]]+)\]\(((?:https?:\/\/|file:\/\/|workspace:\/\/|session:\/\/|mailto:|tel:)[^\s\)]+)\)/g,(_,lb,u)=>{_link_stash.push(_markdownAnchor(lb,u));return `\x00L${_link_stash.length-1}\x00`;});
    t=t.replace(/(https?:\/\/[^\s<>"')\]]+)/g,(url)=>{const trail=url.match(/[.,;:!?)]$/)?url.slice(-1):'';const clean=trail?url.slice(0,-1):url;return `<a href="${clean}" target="_blank" rel="noopener">${esc(clean)}</a>${trail}`;});
    t=t.replace(/\x00L(\d+)\x00/g,(_,i)=>_link_stash[+i]);
    t=t.replace(/\x00G(\d+)\x00/g,(_,i)=>_img_stash[+i]);
    // Escape any plain text that isn't already wrapped in a tag we produced
    // by escaping bare < > that are not part of our own tags
    const SAFE_INLINE=/^<\/?(strong|em|del|code|a|img)([\s>]|$)/i;
    t=t.replace(/<\/?[a-z][^>]*>/gi,tag=>SAFE_INLINE.test(tag)?tag:esc(tag));
    return t;
  }
  // Stash <code> tags from the backtick pass above so the outer bold/italic
  // regexes don't esc() their content (e.g. **`code`** → <strong><code>code</code></strong>)
  const _ob_stash=[];
  s=s.replace(/(<code\b[^>]*>[\s\S]*?<\/code>)/g,m=>{_ob_stash.push(m);return `\x00O${_ob_stash.length-1}\x00`;});
  s=s.replace(/\*\*\*(.+?)\*\*\*/g,(_,t)=>`<strong><em>${esc(t)}</em></strong>`);
  s=s.replace(/\*\*(.+?)\*\*/g,(_,t)=>`<strong>${esc(t)}</strong>`);
  s=s.replace(/\*([^*\n]+)\*/g,(_,t)=>`<em>${esc(t)}</em>`);
  s=s.replace(/~~(.+?)~~/g,(_,t)=>`<del>${esc(t)}</del>`);
  s=s.replace(/\x00O(\d+)\x00/g,(_,i)=>_ob_stash[+i]);
  s=s.replace(/^###### (.+)$/gm,(_,t)=>`<h6>${inlineMd(t)}</h6>`).replace(/^##### (.+)$/gm,(_,t)=>`<h5>${inlineMd(t)}</h5>`).replace(/^#### (.+)$/gm,(_,t)=>`<h4>${inlineMd(t)}</h4>`).replace(/^### (.+)$/gm,(_,t)=>`<h3>${inlineMd(t)}</h3>`).replace(/^## (.+)$/gm,(_,t)=>`<h2>${inlineMd(t)}</h2>`).replace(/^# (.+)$/gm,(_,t)=>`<h1>${inlineMd(t)}</h1>`);
  s=s.replace(/^---+$/gm,'<hr>');
  // (Blockquotes are handled by the pre-pass at the top of renderMd, before
  // fence_stash. The per-line passes below never see > prefixes.)
  // B8: improved list handling supporting up to 2 levels of indentation
  s=s.replace(/((?:^(?:  )?[-*+] .+\n?)+)/gm,block=>{
    const lines=block.trimEnd().split('\n');
    let html='<ul>';
    for(const l of lines){
      const indent=/^ {2,}/.test(l);
      const text=l.replace(/^ {0,4}[-*+] /,'');
      let _ih;
      if(/^\[x\] /i.test(text)) _ih='<span class="task-done">✅</span> '+inlineMd(text.slice(4));
      else if(/^\[ \] /.test(text)) _ih='<span class="task-todo">☐</span> '+inlineMd(text.slice(4));
      else _ih=inlineMd(text);
      if(indent) html+=`<li style="margin-left:16px">${_ih}</li>`;
      else html+=`<li>${_ih}</li>`;
    }
    return html+'</ul>';
  });
  // Ordered lists: use value= on each <li> so the correct number is preserved
  // even when blank lines between items cause the paragraph splitter to place
  // each item in its own <ol> container — without value= every <ol> restarts
  // at 1, producing "1. 1. 1." instead of "1. 2. 3." (#886).
  s=s.replace(/((?:^(?:  )?\d+\. .+\n?)+)/gm,block=>{
    const lines=block.trimEnd().split('\n');
    let html='<ol>';
    for(const l of lines){
      const numMatch=l.match(/^\s*(\d+)\. /);
      const num=numMatch?parseInt(numMatch[1],10):null;
      const text=l.replace(/^ {0,4}\d+\. /,'');
      const valAttr=num!==null?` value="${num}"`:'';
      html+=`<li${valAttr}>${inlineMd(text)}</li>`;
    }
    return html+'</ol>';
  });
  // Tables: | col | col | header row followed by | --- | --- | separator then data rows
  // NOTE: table pass runs BEFORE outer link pass so [label](url) in table cells
  // is handled by inlineMd() only — prevents double-linking.
  s=s.replace(/((?:^\|.+\|\n?)+)/gm,block=>{
    const rows=block.trim().split('\n').filter(r=>r.trim());
    if(rows.length<2)return block;
    const isSep=r=>/^\|[\s|:-]+\|$/.test(r.trim());
    if(!isSep(rows[1]))return block;
    // _protectPipes: temporarily swap pipes inside matching bracket pairs for a
    // sentinel before split('|'), then restore. Iterates until no more matches
    // so all pipes inside one pair are caught.
    // Note: both opening and closing brace literals in the character classes
    // are written as hex escapes (\x7b and \x7d) so the JS source contains no
    // bare brace glyphs that would confuse the brace-counting extractFunc in
    // tests/test_renderer_js_behaviour.py. Regex semantics are identical.
    // Bracket set is paren / square / curly only -- NOT angle brackets, since
    // angle brackets are overwhelmingly comparison operators in real LLM table
    // output (`| x < 5 | y > 10 |`) and treating them as a pair collapses cells.
    const _protectPipes=r=>{let prev;do{prev=r;r=r.replace(/([([\x7b][^)\]\x7d]*)[|]([^)\]\x7d]*[)\]\x7d])/g,(_,a,b)=>a+'\x00PIPE\x00'+b);}while(r!==prev);return r;};
    const _restorePipes=s=>s.replace(/\x00PIPE\x00/g,'|');
    const parseRow=r=>{r=_protectPipes(r);return r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<td>${inlineMd(_restorePipes(c.trim()))}</td>`).join('');};
    const parseHeader=r=>{r=_protectPipes(r);return r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<th>${inlineMd(_restorePipes(c.trim()))}</th>`).join('');};
    const header=`<tr>${parseHeader(rows[0])}</tr>`;
    const body=rows.slice(2).map(r=>`<tr>${parseRow(r)}</tr>`).join('');
    // Surround with blank lines so the final paragraph splitter treats the
    // generated table as its own block even when the regex consumes one of the
    // markdown block's trailing newlines.
    return `\n\n<table><thead>${header}</thead><tbody>${body}</tbody></table>\n\n`;
  });
  // #487: Outer image pass — handles ![alt](url) in plain paragraphs (outside tables/lists).
  // Runs AFTER the table pass (images in table cells are handled by inlineMd() above).
  // Runs BEFORE the outer [label](url) link pass so the image is not consumed as a plain link.
  s=s.replace(/!\[([^\]]*)\]\((https?:\/\/[^\)]+)\)/g,(_,alt,url)=>`<img src="${url.replace(/"/g,'%22')}" alt="${esc(alt)}" class="msg-media-img" loading="lazy">`);
  // Outer link pass for labeled links in plain paragraphs (outside table cells).
  // Runs AFTER the table pass so table cells are processed by inlineMd() only.
  // Stash existing <a> tags first to avoid re-linking already-linked URLs.
  const _a_stash=[];
  s=s.replace(/(<a\b[^>]*>[\s\S]*?<\/a>)/g,m=>{_a_stash.push(m);return `\x00A${_a_stash.length-1}\x00`;});
  s=s.replace(/\[([^\]]+)\]\(((?:https?:\/\/|file:\/\/|workspace:\/\/|session:\/\/|mailto:|tel:)[^\s\)]+)\)/g,(_,label,url)=>_markdownAnchor(label,url));
  s=s.replace(/\x00A(\d+)\x00/g,(_,i)=>_a_stash[+i]);
  // Restore raw <pre> only after markdown rewrites so literal preformatted
  // content stays placeholder-protected, then let the sanitizer normalize tags.
  s=s.replace(/\x00R(\d+)\x00/g,(_,i)=>rawPreStash[+i]);
  // Sanitize any remaining HTML tags.  The renderer intentionally returns
  // HTML and inserts it with innerHTML later, so tag names alone are not enough:
  // raw/model-provided HTML like <img onerror=...> or <a href="javascript:...">
  // must lose executable attributes and dangerous schemes while preserving the
  // small set of attributes generated by this markdown pipeline.
  // Reference only — documents the allowed tag set. Superseded by _tag() allowlists.
  // Tests verify this list is complete; _tag() enforces it.
  const SAFE_TAGS=/^<\/?(?:strong|em|del|code|pre|h[1-6]|ul|ol|li|table|thead|tbody|tr|th|td|hr|blockquote|p|br|a|div|span|img)([\s>]|$)/i;
  function _safeAttrValue(v){
    return String(v||'').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&amp;/g,'&').trim();
  }
  function _markdownHref(raw){
    const href=String(raw||'').replace(/"/g,'%22');
    if(/^session:\/\//i.test(href)){
      const sid=href.replace(/^session:\/\//i,'').split(/[?#]/)[0];
      try{
        const decoded=decodeURIComponent(sid);
        if(typeof _sessionUrlForSid==='function') return _sessionUrlForSid(decoded);
        return 'session/'+encodeURIComponent(decoded);
      }catch(_){
        return 'session/'+encodeURIComponent(sid);
      }
    }
    if(/^workspace:\/\//i.test(href)){
      try{
        const rel=decodeURIComponent(href.replace(/^workspace:\/\//i,'')).replace(/^~\//,'').replace(/^\.\//,'');
        return '#workspace='+encodeURIComponent(rel);
      }catch(_){
        return '#';
      }
    }
    if(/^file:\/\//i.test(href)){
      try{
        const path=decodeURIComponent(href.replace(/^file:\/\//i,''));
        return 'api/media?path='+encodeURIComponent(path)+'&inline=1';
      }catch(_){
        return 'api/media?path='+encodeURIComponent(href.replace(/^file:\/\//i,''))+'&inline=1';
      }
    }
    return href;
  }
  function _isInternalSessionHref(raw){
    const href=String(raw||'').trim();
    if(/^session\/[^?#]+/i.test(href)) return true;
    try{
      const base=(typeof document!=='undefined'&&document.baseURI)||
        (typeof window!=='undefined'&&window.location&&window.location.href)||
        'http://localhost/';
      const url=new URL(href,base);
      const baseUrl=new URL(base,base);
      if(url.origin!==baseUrl.origin) return false;
      const basePath=baseUrl.pathname.replace(/(?:index\.html)?$/,'').replace(/\/[^/]*$/,'/');
      const root=basePath.endsWith('/')?basePath:basePath+'/';
      return url.pathname.startsWith(root+'session/')||url.pathname.startsWith('/session/');
    }catch(_){
      return false;
    }
  }
  function _markdownAnchor(label,rawUrl){
    const href=_markdownHref(rawUrl);
    const internal=/^session:\/\//i.test(String(rawUrl||'')) || _isInternalSessionHref(href);
    return `<a${internal?' class="session-link"':''} href="${href}"${internal?'':' target="_blank" rel="noopener"'}>${esc(label)}</a>`;
  }
  function _isSafeUrl(v, img){
    const raw=_safeAttrValue(v);
    const compact=raw.replace(/[\u0000-\u001f\u007f\s]+/g,'').toLowerCase();
    if(!compact) return false;
    if(/^(javascript|data|vbscript):/i.test(compact)) return false;
    if(/^https?:\/\//i.test(raw)) return true;
    if(/^(mailto:|tel:)/i.test(raw)) return true;
    if(img && /^api\//i.test(raw)) return true;
    if(!img && (/^api\//i.test(raw) || /^#/.test(raw) || _isInternalSessionHref(raw))) return true;
    return false;
  }
  function _attrs(raw){
    const out={};
    String(raw||'').replace(/([a-zA-Z0-9:_-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>`]+)))?/g,(_,k,dq,sq,bare)=>{
      out[String(k).toLowerCase()]=dq!==undefined?dq:(sq!==undefined?sq:(bare!==undefined?bare:''));
      return '';
    });
    return out;
  }
  function _cls(v, allowed){
    const got=String(v||'').split(/\s+/).filter(c=>allowed.includes(c));
    return got.length?` class="${esc(got.join(' '))}"`:'';
  }
  function _tag(tag){
    const m=String(tag||'').match(/^<\s*(\/)?\s*([a-zA-Z][\w:-]*)([\s\S]*?)(\/)?\s*>$/);
    if(!m) return esc(tag);
    const closing=!!m[1];
    const name=m[2].toLowerCase();
    const rawAttrs=m[3]||'';
    const plain=['strong','em','del','pre','h1','h2','h3','h4','h5','h6','ul','ol','table','thead','tbody','tr','th','td','blockquote','p','br','hr'];
    if(closing) return plain.includes(name)||['a','div','span','li','code'].includes(name)?`</${name}>`:'';
    if(name==='code'){
      const a=_attrs(rawAttrs);
      const cls=/^language-[a-z0-9_+-]+$/i.test(a.class||'')?` class="${esc(a.class)}"`:'';
      return `<code${cls}>`;
    }
    if(plain.includes(name)) return `<${name}>`;
    const a=_attrs(rawAttrs);
    if(name==='li'){
      const value=/^\d+$/.test(a.value||'')?` value="${esc(a.value)}"`:'';
      const style=(a.style||'').replace(/\s+/g,'').toLowerCase()==='margin-left:16px'?` style="margin-left:16px"`:'';
      return `<li${value}${style}>`;
    }
    if(name==='span'){
      return `<span${_cls(a.class,['task-done','task-todo','katex-inline'])}${a['data-katex']==='inline'?' data-katex="inline"':''}>`;
    }
    if(name==='div'){
      const cls=_cls(a.class,['pre-header','mermaid-block','katex-block']);
      const mermaid=a['data-mermaid-id']?` data-mermaid-id="${esc(a['data-mermaid-id'])}"`:'';
      const katex=a['data-katex']==='display'?' data-katex="display"':'';
      return `<div${cls}${mermaid}${katex}>`;
    }
    if(name==='a'){
      if(!_isSafeUrl(a.href,false)) return '<a>';
      const target=a.target==='_blank'?' target="_blank"':'';
      const rel=a.rel==='noopener'?' rel="noopener"':'';
      const cls=_cls(a.class,['msg-media-link','skill-linked-file','skill-file-back','session-link']);
      const download=a.download?` download="${esc(a.download)}"`:'';
      return `<a${cls} href="${esc(_safeAttrValue(a.href))}"${target}${rel}${download}>`;
    }
    if(name==='img'){
      if(!_isSafeUrl(a.src,true)) return '';
      const cls=_cls(a.class,['msg-media-img']);
      const alt=` alt="${esc(_safeAttrValue(a.alt||''))}"`;
      const loading=a.loading==='lazy'?' loading="lazy"':'';
      return `<img${cls} src="${esc(_safeAttrValue(a.src))}"${alt}${loading}>`;
    }
    return '';
  }
  s=s.replace(/<\/?[a-z][^>]*>/gi,tag=>_tag(tag));
  // Incomplete raw tags must not survive until paragraph wrapping, where the
  // renderer's generated </p> could provide a closing ">" and turn them into
  // executable HTML in innerHTML (for example: <img src=x onerror=...//).
  s=s.replace(/<[a-zA-Z][\w:-]*[^>\n]*$/gm,tag=>esc(tag));
  // Autolink: convert plain URLs to clickable links.
  // Stash <a>, <img> and <pre> blocks so autolink never runs inside them.
  const _al_stash=[];
  s=s.replace(/(<a\b[^>]*>[\s\S]*?<\/a>|<img\b[^>]*>|<pre\b[^>]*>[\s\S]*?<\/pre>)/g,m=>{_al_stash.push(m);return `\x00B${_al_stash.length-1}\x00`;});
  s=s.replace(/(https?:\/\/[^\s<>"'\)\]]+)/g,(url)=>{
    // Strip trailing punctuation that was likely not part of the URL
    const trail=url.match(/[.,;:!?)]$/)?url.slice(-1):'';
    const clean=trail?url.slice(0,-1):url;
    return `<a href="${clean}" target="_blank" rel="noopener">${esc(clean)}</a>${trail}`;
  });
  s=s.replace(/\x00B(\d+)\x00/g,(_,i)=>_al_stash[+i]);
  // Restore math stash → katex placeholder spans/divs
  // These will be rendered by renderKatexBlocks() after DOM insertion
  s=s.replace(/\x00M(\d+)\x00/g,(_,i)=>{
    const item=math_stash[+i];
    if(item.type==='display'){
      return `<div class="katex-block" data-katex="display">${esc(item.src)}</div>`;
    }
    return `<span class="katex-inline" data-katex="inline">${esc(item.src)}</span>`;
  });
  // Restore fenced block stash (\x00P) → <pre><code> HTML.
  // Happens AFTER all markdown passes (lists, headings, tables, etc.) so
  // diff/patch content inside code blocks is never misinterpreted as markdown.
  // The _pre_stash below then protects these blocks from paragraph splitting.
  s=s.replace(/\x00P(\d+)\x00/g,(_,i)=>_preBlock_stash[+i]);
  // Stash rendered <pre> blocks (with optional pre-header div) and mermaid/katex
  // divs before paragraph splitting so \n inside code blocks is never replaced
  // with <br>. Token \x00E (next free after B D F G L M C O A).
  // Fixes #745: code blocks collapse to single line when not preceded by blank line.
  const _pre_stash=[];
  // #1463 / #1618: regex must match <pre> with ANY attributes — PR #484 added
  // <pre class="tree-raw-view"> for JSON/YAML and <pre class="diff-block"> for
  // diff/patch which the literal-<pre> shape missed. Newlines inside those
  // blocks were falling through to the paragraph wrap below and getting
  // converted to <br>, causing the YAML/JSON/diff collapse. PR #1516's CSS
  // fix targeted the wrong layer (Prism token white-space) — by the time it
  // ran, the \n had already been replaced. The CSS rule is kept as defense
  // in depth.
  s=s.replace(/(<div class="pre-header">[\s\S]*?<\/div>)?<pre[^>]*>[\s\S]*?<\/pre>|<div class="(mermaid-block|katex-block)"[\s\S]*?<\/div>/g,m=>{
    _pre_stash.push(m);
    return '\x00E'+(_pre_stash.length-1)+'\x00';
  });
  const parts=s.split(/\n{2,}/);
  s=parts.map(p=>{p=p.trim();if(!p)return '';if(/^<(h[1-6]|ul|ol|table|pre|hr|blockquote)|^\x00[EQ]/.test(p))return p;return `<p>${p.replace(/\n/g,'<br>')}</p>`;}).join('\n');
  s=s.replace(/\x00E(\d+)\x00/g,(_,i)=>_pre_stash[+i]);
  // ── Restore MEDIA stash → inline images or download links ─────────────────
  s=s.replace(/\x00D(\d+)\x00/g,(_,i)=>{
    let ref=media_stash[+i];
    // Keep this logic self-contained: some tests extract renderMd() alone and
    // execute it in node, without the top-level helper functions from ui.js.
    const mediaKindForName=(name='')=>{
      const clean=String(name||'').split('?')[0].toLowerCase();
      if(/\.(mp3|wav|m4a|aac|ogg|oga|opus|flac)$/i.test(clean)) return 'audio';
      if(/\.(mp4|mov|m4v|webm|ogv|avi|mkv)$/i.test(clean)) return 'video';
      if(_IMAGE_EXTS.test(clean)) return 'image';
      return '';
    };
    const mediaPlayerHtml=(kind,src,name)=>{
      if(typeof _mediaPlayerHtml==='function') return _mediaPlayerHtml(kind,src,name);
      const safeName=esc(name||kind||'media');
      const safeSrc=esc(src);
      const tag=kind==='video'
        ? `<video class="msg-media-player msg-media-video" src="${safeSrc}" controls preload="metadata" playsinline title="${safeName}"></video>`
        : `<audio class="msg-media-player msg-media-audio" src="${safeSrc}" controls preload="metadata" title="${safeName}"></audio>`;
      return `<div class="msg-media-editor msg-media-editor--${kind}" data-media-kind="${kind}">${tag}<div class="msg-media-meta"><span class="msg-media-name">${safeName}</span></div></div>`;
    };
    const localArtifactCard=(src,name)=>{
      const safeSrc=esc(src);
      const safeName=esc(name||'image');
      const tt=(typeof t==='function')?t:(key=>({media_download:'Download'}[key]||key));
      // Clean inline image (keeps the existing .msg-media-img lightbox-on-click
      // behavior) with a hover/focus-revealed Download action overlaid top-right,
      // matching the ChatGPT/Claude/Gemini pattern. The image stays the hero —
      // no permanent card chrome. Download is the one affordance the lightbox
      // (zoom-on-click) doesn't already provide.
      const dlLabel=esc(tt('media_download'));
      const dlSvg='<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>';
      return `<span class="msg-artifact-image"><img class="msg-media-img" src="${safeSrc}" alt="${safeName}" loading="lazy"><a class="msg-artifact-download" href="${safeSrc}" download="${safeName}" title="${dlLabel}" aria-label="${dlLabel}" onclick="event.stopPropagation()">${dlSvg}</a></span>`;
    };
    if(/^file:\/\//i.test(ref)){
      try{
        const u=new URL(ref);
        ref=decodeURIComponent(u.pathname||ref.replace(/^file:\/\//i,''));
      }catch(_){
        try{ref=decodeURIComponent(ref.replace(/^file:\/\//i,''));}
        catch(__){ref=ref.replace(/^file:\/\//i,'');}
      }
    }
    // HTTP(S) URL
    if(/^https?:\/\//i.test(ref)){
      // Rewrite localhost/127.0.0.1 to the actual server base URL so remote
      // users (VPN, Docker, deployed) can load agent-generated images (#642).
      // Strip the trailing slash from document.baseURI so the URL's own path
      // joins cleanly — this preserves any subpath mount (e.g. /hermes/).
      let src=ref;
      if(/^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?/i.test(src)){
        const base=(document.baseURI||'').replace(/\/$/,'');
        src=src.replace(/^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?/i,base);
      }
      // MEDIA: tokens are usually tool-generated images. Render all https://
      // URLs as <img> so extensionless CDN paths still work (#853), while
      // preserving explicit audio/video/SVG URLs with their proper handlers.
      const urlPath=src.split('?')[0];
      const mediaKind=mediaKindForName(urlPath);
      // SVG URLs → render inline as image
      if(_SVG_EXTS.test(urlPath)){
        return `<img class="msg-media-svg" src="${esc(src)}" alt="${t('media_svg_label')}" loading="lazy">`;
      }
      if(mediaKind==='audio'||mediaKind==='video') return mediaPlayerHtml(mediaKind,src,urlPath.split('/').pop()||mediaKind);
      // Render all https:// URLs as <img> — extensionless CDN paths like fal.media still work (#853)
      if(_IMAGE_EXTS.test(urlPath) || /^https?:\/\//i.test(src)){
        return `<img class="msg-media-img" src="${esc(src)}" alt="image" loading="lazy">`;
      }
      return `<a href="${esc(src)}" target="_blank" rel="noopener">${esc(src)}</a>`;
    }
    // Local file path
    const mediaSessionId=(typeof S!=='undefined'&&S&&S.session&&S.session.session_id)?String(S.session.session_id):'';
    const apiUrl='api/media?path='+encodeURIComponent(ref)+(mediaSessionId?'&session_id='+encodeURIComponent(mediaSessionId):'');
    const localKind=mediaKindForName(ref);
    if(localKind==='image'){
      return localArtifactCard(apiUrl,ref.split('/').pop()||'image');
    }
    // SVG → inline image (no download, render directly)
    if(_SVG_EXTS.test(ref)){
      return `<img class="msg-media-svg" src="${esc(apiUrl)}" alt="${t('media_svg_label')}" loading="lazy">`;
    }
    // Audio/video → inline player with speed controls; use &inline=1 for byte-range seeking
    if(_AUDIO_EXTS.test(ref)||_VIDEO_EXTS.test(ref)){
      const kind=_AUDIO_EXTS.test(ref)?'audio':'video';
      return _mediaPlayerHtml(kind,apiUrl+'&inline=1',ref.split('/').pop()||ref);
    }
    // PDF files → render first page preview with lazy-load
    if(_PDF_EXTS.test(ref)){
      const fname=esc(ref.split('/').pop()||ref);
      return `<div class="pdf-preview-load" data-path="${esc(ref)}"><span class="pdf-preview-spinner">⏳</span> ${t('pdf_loading')} ${fname}...</div>`;
    }
    // HTML files → render inline in sandboxed iframe with lazy-load
    if(_HTML_EXTS.test(ref)){
      return `<div class="html-preview-load" data-path="${esc(ref)}"><span class="html-preview-spinner">⏳</span> ${t('html_loading')}</div>`;
    }
    // .patch/.diff files → render inline as colored diff instead of download
    const fname=esc(ref.split('/').pop()||ref);
    if(/\.(patch|diff)$/i.test(ref)){
      return `<div class="diff-inline-load" data-path="${esc(ref)}">${t('diff_loading')} ${fname}...</div>`;
    }
    // CSV files → lazy-load and render as table
    if(_CSV_EXTS.test(ref)){
      return `<div class="csv-inline-load" data-path="${esc(ref)}">${t('csv_loading')} ${fname}...</div>`;
    }
    // Excalidraw files → lazy-load inline embed
    if(_EXCALIDRAW_EXTS.test(ref)){
      return `<div class="excalidraw-inline-load" data-path="${esc(ref)}">${t('excalidraw_loading')} ${fname}...</div>`;
    }
    return `<a class="msg-media-link" href="${esc(apiUrl+'&download=1')}" download="${fname}">📎 ${fname}</a>`;
  });

  // ── End MEDIA restore ──────────────────────────────────────────────────────
  // Restore blockquote stash. Done last so the inner HTML (already produced
  // by the recursive renderMd in the pre-pass) is dropped into the final
  // string verbatim — no further passes can mangle it.
  s=s.replace(/\x00Q(\d+)\x00/g,(_,i)=>_bq_stash[+i]);
  return s;
}

function _stripAttachedFilesMarkerForDisplay(text){
  return String(text||'').replace(/\n\n\[Attached files: [^\]]+\]$/,'').trim();
}

function setStatus(t){
  if(!t)return;
  showToast(t, 4000);
}

function setComposerStatus(t){
  const el=$('composerStatus');
  if(!el)return;
  if(!t){
    el.style.display='none';
    el.textContent='';
    return;
  }
  el.textContent=t;
  el.style.display='';
}

let _composerLockState=null;
let _compressionPlaceholderSaved=null;

function lockComposerForClarify(placeholderText){
  const input=$('msg');
  if(!input) return;
  // Save the current composer text as a server-side draft before locking,
  // so the user's draft is preserved if they switch sessions while a clarify
  // card is active (and survives page refresh / syncs across clients).
  const sid = S && S.session && S.session.session_id;
  if (sid && typeof _saveComposerDraftNow === 'function') {
    _saveComposerDraftNow(sid, input.value || '', S.pendingFiles ? [...S.pendingFiles] : []);
  }
  if(!_composerLockState){
    _composerLockState={
      disabled: input.disabled,
      placeholder: input.placeholder,
    };
  }
  input.disabled=true;
  if(placeholderText) input.placeholder=placeholderText;
  updateSendBtn();
}

function unlockComposerForClarify(){
  const input=$('msg');
  if(!input) return;
  if(_composerLockState){
    input.disabled=!!_composerLockState.disabled;
    if(typeof _composerLockState.placeholder==='string'){
      input.placeholder=_composerLockState.placeholder;
    }
    _composerLockState=null;
  }else{
    input.disabled=false;
  }
  updateSendBtn();
}

function _composerHasContent(){
  const msg=$('msg');
  return !!((msg&&msg.value.trim().length>0)||S.pendingFiles.length>0);
}

function _getExplicitBusyCommandAction(text){
  const trimmed=(text||'').trim();
  if(!trimmed.startsWith('/')) return null;
  const body=trimmed.slice(1);
  const name=(body.split(/\s+/)[0]||'').toLowerCase();
  const args=body.slice(name.length).trim();
  if(!args) return null;
  if(name==='queue') return 'queue';
  if(name==='steer'){
    if(S.activeStreamId&&typeof _trySteer==='function') return 'steer';
    return 'queue';
  }
  if(name==='interrupt'){
    if(S.activeStreamId&&typeof cancelStream==='function') return 'interrupt';
    return 'queue';
  }
  return null;
}

function getComposerPrimaryAction(){
  const msg=$('msg');
  const hasContent=_composerHasContent();
  const locked=!!(msg&&msg.disabled);
  if(locked) return 'disabled';
  const compressionRunning=typeof isCompressionUiRunning==='function'&&isCompressionUiRunning();
  const isBusy=!!S.busy||compressionRunning;
  if(!isBusy) return hasContent?'send':'disabled';
  if(!hasContent){
    if(S.activeStreamId&&typeof cancelStream==='function') return 'stop';
    if(compressionRunning) return 'queue';
    return 'disabled';
  }
  const explicitAction=_getExplicitBusyCommandAction(msg&&msg.value);
  if(explicitAction) return explicitAction;
  const busyMode=window._busyInputMode||'queue';
  if(busyMode==='steer'){
    if(S.activeStreamId&&typeof _trySteer==='function') return 'steer';
    return 'queue';
  }
  if(busyMode==='interrupt'){
    if(S.activeStreamId&&typeof cancelStream==='function') return 'interrupt';
    return 'queue';
  }
  return 'queue';
}

function _setComposerPrimaryButtonIcon(btn,action){
  // Queue/interrupt/steer icons are inline Lucide SVGs (ISC):
  // https://lucide.dev/icons/
  const icons={
    send:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>',
    queue:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 5H3"/><path d="M16 12H3"/><path d="M9 19H3"/><path d="m16 16-3 3 3 3"/><path d="M21 5v12a2 2 0 0 1-2 2h-6"/></svg>',
    interrupt:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 4v16"/><path d="M6.029 4.285A2 2 0 0 0 3 6v12a2 2 0 0 0 3.029 1.715l9.997-5.998a2 2 0 0 0 .003-3.432z"/></svg>',
    steer:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="m16.24 7.76-1.804 5.411a2 2 0 0 1-1.265 1.265L7.76 16.24l1.804-5.411a2 2 0 0 1 1.265-1.265z"/></svg>',
    stop:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"></rect></svg>',
    disabled:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>'
  };
  const next=icons[action]||icons.send;
  if(btn.innerHTML!==next) btn.innerHTML=next;
}

function updateSendBtn(){
  const btn=$('btnSend');
  if(!btn) return;
  const action=getComposerPrimaryAction();
  btn.dataset.action=action;
  btn.classList.toggle('stop',action==='stop');
  btn.classList.toggle('queue',action==='queue');
  btn.classList.toggle('interrupt',action==='interrupt');
  btn.classList.toggle('steer',action==='steer');
  const _tt=(key,fb)=>{if(typeof t!=='function')return fb;const val=t(key);return val===key?fb:(val||fb);};
  let _btnTitle;
  if(action==='disabled'){
    const _dmsg=$('msg');
    if(_dmsg&&_dmsg.disabled) _btnTitle=_tt('composer_disabled_clarify','Respond to the clarification request');
    else _btnTitle=_tt('composer_disabled_empty','Type a message to send');
  }else if(action==='queue'&&typeof isCompressionUiRunning==='function'&&isCompressionUiRunning()){
    _btnTitle=_tt('composer_compression_will_queue','Type a message — it will queue and send after compression');
  }else{
    const _tmap={send:'Send message',queue:'Queue message',interrupt:'Interrupt and send',steer:'Steer current response',stop:'Stop generation'};
    _btnTitle=_tt('composer_'+action,_tmap[action]||'Send message');
  }
  btn.title=_btnTitle;
  btn.setAttribute('aria-label',_btnTitle);
  _setComposerPrimaryButtonIcon(btn,action);
  // Single primary action button: while busy/no-draft it becomes the red Stop
  // action; while busy with a draft it reflects queue/interrupt/steer.
  btn.style.display='';
  btn.disabled=action==='disabled';
  if(action!=='disabled'&&!btn.classList.contains('visible')){
    btn.classList.remove('visible');
    requestAnimationFrame(()=>btn.classList.add('visible'));
  } else if(action==='disabled'){
    btn.classList.remove('visible');
  }
}

async function handleComposerPrimaryAction(){
  if(window._micActive){
    window._micPendingSend=true;
    _stopMic();
    return;
  }
  const action=typeof getComposerPrimaryAction==='function'?getComposerPrimaryAction():'send';
  if(action==='disabled') return;
  if(action==='stop'){
    if(typeof cancelStream==='function') await cancelStream();
    return;
  }
  await send();
}

function setBusy(v){
  S.busy=v;
  updateSendBtn();
  if(!v){
    if(typeof _clearActivityElapsedTimer==='function') _clearActivityElapsedTimer();
    setStatus('');
    setComposerStatus('');
    const sid=_queueDrainSid||(S.session&&S.session.session_id);
    _queueDrainSid=null;
    updateQueueBadge(sid);
    // Drain one queued message for the finished session after UI settles
    const _isViewedSid=!S.session||sid===S.session.session_id;
    const next=sid&&_isViewedSid?shiftQueuedSessionMessage(sid):null;
    if(next){
      updateQueueBadge(sid);
      setTimeout(()=>{
        // Guard: if the user switched away from the drain session during
        // the 120ms settle window, the queued message must NOT go to the
        // wrong chat.  Put it back into the original session's queue and
        // skip sending — it will drain when the user returns to that session
        // or when its next stream completes while it is the active view.
        if(S.session&&S.session.session_id!==sid){
          queueSessionMessage(sid,next);
          updateQueueBadge(sid);
          return;
        }
        $('msg').value=next.text||'';
        S.pendingFiles=Array.isArray(next.files)?[...next.files]:[];
        // Restore model from queued item (sent in /api/chat/start payload)
        // Note: profile is NOT restored — full profile switch requires server interaction
        if(next.model&&S.session&&next.model!==S.session.model){
          S.session.model=next.model;
        }
        if(next.model_provider&&S.session) S.session.model_provider=next.model_provider;
        if(next.model&&S.session){
          if(typeof _applyModelToDropdown==='function'&&$('modelSelect')) _applyModelToDropdown(next.model,$('modelSelect'),S.session.model_provider||null);
          if(typeof syncModelChip==='function') syncModelChip();
        }
        autoResize();
        renderTray();
        send();
      },120);
    }
  }
}

// ── Queue chip display (Codex Desktop pattern) ─────────────────────────────
// Queued messages appear as chips inside #queueChips (above the textarea)
// while pending. When the session fires the queued message it becomes a
// normal user bubble in the chat — the chip is removed at drain time.
const _queueRenderKeys={};  // per-session fingerprint to avoid redundant rebuilds
const _queueCollapsed={};   // per-session: true when user explicitly collapsed the card

function _renderQueueChips(sid){
  const card=document.getElementById('queueCard');
  const inner=document.getElementById('queueChips');
  if(!card||!inner) return;
  const q=_getSessionQueue(sid,false);
  const key=q.map(e=>{const t=e&&(e.text||e.message||e.content||'');return(e&&e._queued_at||0)+':'+t.length+':'+t.slice(0,20);}).join('|');
  if(key===(_queueRenderKeys[sid]||'')&&key!='') return;
  // Skip re-render if user is actively editing inside the queue panel
  if(inner.contains(document.activeElement)&&document.activeElement!==inner) return;
  _queueRenderKeys[sid]=key;
  inner.innerHTML='';
  if(!q.length){
    card.classList.remove('visible');
    const _msgs=document.getElementById('messages');
    if(_msgs) _msgs.classList.remove('queue-open');
    return;
  }
  // Respect user-collapsed state — don't reopen if user explicitly hid the card
  if(_queueCollapsed[sid]){
    // Update chips content without showing card (so data is fresh if user re-expands)
    inner.innerHTML='';
    // fall through to render rows into inner but skip making card visible
  } else {
    card.classList.add('visible');
  }
  // Push messages area up so content isn't hidden behind the flyout
  const _msgs=document.getElementById('messages');
  if(_msgs&&!_queueCollapsed[sid]){
    _msgs.classList.add('queue-open');
    // Measure after 350ms transition completes (not mid-animation — height would be wrong)
    setTimeout(()=>{
      if(!card.classList.contains('visible')) return;
      const h=card.getBoundingClientRect().height;
      if(h>0) _msgs.style.setProperty('--queue-card-height', h+'px');
      if(S.activeStreamId&&typeof scrollIfPinned==='function') scrollIfPinned();
      else if(!S.activeStreamId&&typeof scrollToBottom==='function') scrollToBottom();
    }, 360);
  }

  function _saveAndRefresh(){
    const liveQ=_getSessionQueue(sid,false);
    if(!liveQ.length){delete SESSION_QUEUES[sid];try{sessionStorage.removeItem('hermes-queue-'+sid);}catch(_){}}
    else{SESSION_QUEUES[sid]=[...liveQ];try{sessionStorage.setItem('hermes-queue-'+sid,JSON.stringify(liveQ));}catch(_){}}
    delete _queueRenderKeys[sid];
    updateQueueBadge(sid);
  }

  // Header (2+ items)
  if(q.length>1){
    const header=document.createElement('div');
    header.className='queue-card-header';
    const lbl=document.createElement('span');
    lbl.textContent=typeof t==='function'?t('queued_count',q.length):(q.length===1?'1 queued':`${q.length} queued`);
    lbl.title='Sends automatically after the current response completes';
    const actions=document.createElement('span');
    actions.className='queue-card-header-actions';
    const hasFiles=q.some(e=>e&&Array.isArray(e.files)&&e.files.length>0);
    const mergeBtn=document.createElement('button');
    mergeBtn.className='queue-card-btn';
    mergeBtn.title='Combine all into one message'+(hasFiles?' — attachments will be removed':'');
    mergeBtn.innerHTML=li('layers',12)+'Combine';
    mergeBtn.onclick=()=>{
      const _doMerge=(snapshot)=>{
        const combined=snapshot.map(e=>e&&(e.text||e.message||e.content||'')).filter(Boolean).join('\n\n');
        const liveQ=_getSessionQueue(sid,false);
        const first=snapshot.find(e=>e)||{};
        const firstFiles=(snapshot.find(e=>e&&Array.isArray(e.files)&&e.files.length)||{files:[]}).files;
        liveQ.length=0;liveQ.push({text:combined,files:firstFiles,model:first.model||'',model_provider:first.model_provider||null,_queued_at:Date.now()});
        SESSION_QUEUES[sid]=liveQ;
        try{sessionStorage.setItem('hermes-queue-'+sid,JSON.stringify(liveQ));}catch(_){}
        delete _queueRenderKeys[sid];
        updateQueueBadge(sid);
      };
      if(hasFiles){
        if(typeof showToast==='function') showToast('Attachments on queued items will be removed',2600,'warning');
      }
      // Merge from current live queue (no delay — snapshot + defer caused data-loss races)
      _doMerge([..._getSessionQueue(sid,false)]);
    };
    const clearBtn=document.createElement('button');
    clearBtn.className='queue-card-icon-btn';
    clearBtn.title='Clear all queued messages';
    clearBtn.setAttribute('aria-label','Clear all queued messages');
    clearBtn.innerHTML=li('x',13);
    clearBtn.onclick=()=>{q.length=0;_saveAndRefresh();};
    actions.appendChild(mergeBtn);
    actions.appendChild(clearBtn);
    // Hide button — collapses flyout entirely; queue pill re-shows it
    const hideBtn=document.createElement('button');
    hideBtn.className='queue-card-icon-btn';
    hideBtn.title='Hide queue (click the queue pill to show again)';
    hideBtn.setAttribute('aria-label','Hide queue panel');
    hideBtn.innerHTML=li('chevron-down',14);
    hideBtn.onclick=()=>{
      _queueCollapsed[sid]=true;
      card.classList.remove('visible');
      // Read live count at click time (not stale closure q)
      _updateQueuePill(sid,_getSessionQueue(sid,false).length);
    };
    actions.appendChild(hideBtn);
    header.appendChild(lbl);
    header.appendChild(actions);
    inner.appendChild(header);
  }

  let _dragTs=null;  // use _queued_at timestamp — survives re-renders, not an index
  q.forEach((entry,i)=>{
    const _entryTs=entry&&entry._queued_at;
    const entryText=entry&&(entry.text||entry.message||entry.content||'');
    const _files=entry&&Array.isArray(entry.files)?entry.files.filter(Boolean):[];
    const row=document.createElement('div');
    row.className='queue-card-row';
    row.setAttribute('role','listitem');
    row.setAttribute('draggable','true');
    row.ondragstart=(e)=>{if(_entryTs==null) return;_dragTs=_entryTs;row.style.opacity='.4';e.dataTransfer.effectAllowed='move';};
    row.ondragend=()=>{row.style.opacity='';};
    row.ondragover=(e)=>{e.preventDefault();row.style.background='var(--hover-bg)';};
    row.ondragleave=()=>{row.style.background='';};
    row.ondrop=(e)=>{
      e.preventDefault();row.style.background='';
      if(_dragTs!=null&&_dragTs!==_entryTs){
        const fromIdx=q.findIndex(e=>e&&e._queued_at===_dragTs);
        if(fromIdx!==-1&&fromIdx!==i){const moved=q.splice(fromIdx,1)[0];q.splice(i,0,moved);}
        _dragTs=null;_saveAndRefresh();
      }
    };
    // Drag handle
    const drag=document.createElement('span');
    drag.className='queue-card-drag';
    drag.setAttribute('aria-hidden','true');
    drag.innerHTML=typeof li==='function'?li('list-todo',13):'≡';
    // Inline-editable text
    const msgSpan=document.createElement('span');
    msgSpan.className='queue-card-text';
    msgSpan.setAttribute('contenteditable','true');
    msgSpan.setAttribute('role','textbox');
    msgSpan.setAttribute('aria-label','Queued message — edit in place');
    msgSpan.textContent=entryText||(_files.length?'':'—');
    msgSpan.setAttribute('draggable','false');
    msgSpan.onfocus=()=>{msgSpan.style.overflow='auto';msgSpan.style.whiteSpace='pre-wrap';msgSpan.style.textOverflow='clip';};
    msgSpan.onblur=()=>{
      msgSpan.style.overflow='';msgSpan.style.whiteSpace='';msgSpan.style.textOverflow='';
      const newText=msgSpan.textContent.trim();
      if(newText===''&&!_files.length){ msgSpan.textContent=entryText||'—'; return; }
      if(newText!==entryText){
        const liveQ=_getSessionQueue(sid,false);
        const idx=_entryTs!=null?liveQ.findIndex(e=>e&&e._queued_at===_entryTs):i;
        if(idx!==-1){
          liveQ[idx]={...liveQ[idx],text:newText};
          try{sessionStorage.setItem('hermes-queue-'+sid,JSON.stringify(liveQ));}catch(_){}
          delete _queueRenderKeys[sid];
          updateQueueBadge(sid);
        }
      }
    };
    msgSpan.onkeydown=(e)=>{if(e.key==='Enter'){e.preventDefault();msgSpan.blur();}if(e.key==='Escape'){msgSpan.textContent=entryText||'—';msgSpan.blur();}};
    // Compact badges (files, model, profile)
    const badges=document.createElement('span');
    badges.className='queue-card-badges';
    if(_files.length>0){
      const fb=document.createElement('span');
      fb.className='queue-card-file-badge';
      fb.title=_files.map(f=>f&&f.name||'file').join(', ');
      fb.innerHTML=li('paperclip',11)+_files.length;
      badges.appendChild(fb);
    }
    const _model=entry&&entry.model;
    if(_model){
      const mb=document.createElement('span');
      mb.title='Model: '+_model;
      // Use the app's friendly label system if available
      const _modelLabel=(typeof _dynamicModelLabels!=='undefined'&&_dynamicModelLabels[_model])
        ||_model.split('/').pop().replace(/^(gpt-|claude-3\.?5?-|claude-|gemini-)/,'').replace(/-\d{4}-\d{2}-\d{2}$/,'').slice(0,12);
      mb.textContent=_modelLabel;
      badges.appendChild(mb);
    }
    // Profile badge removed — drain cannot server-switch profiles so badge was misleading
    // Delete button
    const delBtn=document.createElement('button');
    delBtn.className='queue-card-icon-btn';
    delBtn.setAttribute('aria-label',typeof t==='function'?t('queued_cancel'):'Remove queued message');
    delBtn.setAttribute('draggable','false');
    delBtn.title='Remove from queue';
    delBtn.innerHTML=li('x',13);
    delBtn.onclick=()=>{
      const liveQ=_getSessionQueue(sid,false);
      const idx=_entryTs!=null?liveQ.findIndex(e=>e&&e._queued_at===_entryTs):i;
      if(idx!==-1) liveQ.splice(idx,1);
      if(!liveQ.length){delete SESSION_QUEUES[sid];try{sessionStorage.removeItem('hermes-queue-'+sid);}catch(_){}}
      else{SESSION_QUEUES[sid]=[...liveQ];try{sessionStorage.setItem('hermes-queue-'+sid,JSON.stringify(liveQ));}catch(_){}}
      delete _queueRenderKeys[sid];
      updateQueueBadge(sid);
    };
    row.appendChild(drag);
    row.appendChild(msgSpan);
    if(badges.childNodes.length) row.appendChild(badges);
    row.appendChild(delBtn);
    inner.appendChild(row);
  });
}

function _updateQueuePill(sid,count){
  const pill=document.getElementById('queuePill');
  if(!pill) return;
  const pillOuter=pill.parentElement;  // .queue-pill-outer — same wrapper as .queue-card
  const card=document.getElementById('queueCard');
  const flyoutVisible=card&&card.classList.contains('visible');
  if(count>0&&!flyoutVisible){
    const label=typeof t==='function'?t('queued_count',count):(count===1?'1 queued':`${count} queued`);
    pill.innerHTML=(typeof li==='function'?li('list-todo',12):'')+
      `<span class="queue-pill-count">${label}</span>`+
      `<span class="queue-pill-chevron">`+(typeof li==='function'?li('chevron-up',12):'▲')+`</span>`;
    pill.title='Show queued messages';
    if(pillOuter) pillOuter.classList.add('show');
    pill.onclick=()=>{
      delete _queueCollapsed[sid];
      const c=document.getElementById('queueCard');
      if(c){
        c.classList.add('visible');
        setTimeout(()=>{
          const firstFocusable=c.querySelector('.queue-card-text, .queue-card-icon-btn');
          if(firstFocusable) firstFocusable.focus();
        }, 360);
      }
      if(pillOuter) pillOuter.classList.remove('show');
      if(S.activeStreamId&&typeof scrollIfPinned==='function') scrollIfPinned();
      else if(!S.activeStreamId&&typeof scrollToBottom==='function') scrollToBottom();
    };
  } else {
    if(pillOuter) pillOuter.classList.remove('show');
    pill.onclick=null;
  }
}

function updateQueueBadge(sessionId){
  const sid=sessionId||(S.session&&S.session.session_id);
  const count=sid?getQueuedSessionCount(sid):0;
  if(count>0&&S.session&&sid===S.session.session_id){
    _renderQueueChips(sid);
    // If card is visible, hide pill. If card is collapsed, update pill count.
    const _cardEl=document.getElementById('queueCard');
    _updateQueuePill(sid,(_cardEl&&_cardEl.classList.contains('visible'))?0:count);
  } else {
    // Always clean up per-session data
    if(sid){delete _queueRenderKeys[sid];delete _queueCollapsed[sid];}
    // Only wipe global DOM if this is the currently active session
    const isActive=S.session&&sid===S.session.session_id;
    if(isActive){
      const card=document.getElementById('queueCard');
      const chips=document.getElementById('queueChips');
      if(card) card.classList.remove('visible');
      // Defer clear until after slide-out transition so content doesn't vanish mid-animation
      if(chips){const _chips=chips;const _card=card;setTimeout(()=>{if(!_card||!_card.classList.contains('visible'))_chips.innerHTML='';},360);}
      const _msgsEl=document.getElementById('messages');
      if(_msgsEl) _msgsEl.classList.remove('queue-open');
      _updateQueuePill(sid,0);
    }
  }
}
const TOAST_DEFAULT_MS=2800;
const TOAST_ERROR_DEFAULT_MS=20000;
function clearToastDismissTimer(el){if(!el)return;clearTimeout(el._t);el._t=null;}
function setToastDismissTimer(el,duration){if(!el)return;clearToastDismissTimer(el);el._t=setTimeout(()=>{el.classList.remove('show');},duration);}
function copyToastText(btn){
  const el=btn&&btn.closest?btn.closest('#toast'):null;
  const text=el?(el.dataset.toastMessage||el.textContent||''):'';
  const done=()=>{const old=btn.textContent;btn.textContent='Copied';setTimeout(()=>{btn.textContent=old;},1200);};
  _copyText(text).then(done).catch(()=>{});
}
function showToast(msg,ms,type){
  const el=$('toast');if(!el)return;
  const s=String(msg==null?'':msg);let t=type;
  if(!t){const low=s.toLowerCase();if(/fail|error|denied|invalid|unavailable|no active|no workspace match|no model match|no personalities/.test(low))t='error';else if(/warn|queued|takes effect|skipped|fallback/.test(low))t='warning';else if(/saved|created|imported|restored|switched|set to|updated|duplicated|moved to|renamed|deleted|complete|pinned|archived|cleared|stopped/.test(low))t='success';else t='info';}
  const duration=(ms==null)?(t==='error'?TOAST_ERROR_DEFAULT_MS:TOAST_DEFAULT_MS):ms;
  el.className='toast show '+t;
  el.dataset.toastMessage=s;
  if(t==='error') el.innerHTML=`<span class="toast-message">${esc(s)}</span><button class="toast-copy" type="button" data-toast-copy="1" onclick="copyToastText(this);event.stopPropagation()">Copy</button>`;
  else el.textContent=s;
  el.onmouseenter=()=>clearToastDismissTimer(el);
  el.onmouseleave=()=>setToastDismissTimer(el,duration);
  el.onfocusin=()=>clearToastDismissTimer(el);
  el.onfocusout=()=>setToastDismissTimer(el,duration);
  setToastDismissTimer(el,duration);
}

// ── Shared app dialogs ───────────────────────────────────────────────────────
// showConfirmDialog(opts) and showPromptDialog(opts) replace browser-native dialog calls
// throughout the UI. Both return Promises and support: title, message, confirmLabel,
// cancelLabel, danger (confirm only), placeholder/value/inputType (prompt only).

const APP_DIALOG={resolve:null,kind:null,lastFocus:null};
let _appDialogBound=false;

function _isAppDialogOpen(){
  const overlay=$('appDialogOverlay');
  return !!(overlay&&overlay.style.display!=='none');
}

function _getAppDialogFocusable(){
  return [$('appDialogInput'), $('appDialogCancel'), $('appDialogConfirm'), $('appDialogClose')]
    .filter(el=>el&&el.style.display!=='none'&&!el.disabled);
}

function _finishAppDialog(result, restoreFocus=true){
  const overlay=$('appDialogOverlay');
  const dialog=$('appDialog');
  const input=$('appDialogInput');
  const confirmBtn=$('appDialogConfirm');
  const resolve=APP_DIALOG.resolve;
  const lastFocus=APP_DIALOG.lastFocus;
  APP_DIALOG.resolve=null;
  APP_DIALOG.kind=null;
  APP_DIALOG.lastFocus=null;
  if(overlay){overlay.style.display='none';overlay.setAttribute('aria-hidden','true');}
  if(dialog) dialog.setAttribute('role','dialog');
  if(input){input.value='';input.style.display='none';input.placeholder='';}
  if(confirmBtn){confirmBtn.classList.remove('danger');confirmBtn.textContent=t('dialog_confirm_btn');}
  if(restoreFocus&&lastFocus&&typeof lastFocus.focus==='function'){setTimeout(()=>lastFocus.focus(),0);}
  if(resolve) resolve(result);
}

function _ensureAppDialogBindings(){
  if(_appDialogBound) return;
  _appDialogBound=true;
  const overlay=$('appDialogOverlay');
  const cancelBtn=$('appDialogCancel');
  const confirmBtn=$('appDialogConfirm');
  const closeBtn=$('appDialogClose');
  if(overlay){
    overlay.addEventListener('click',e=>{
      if(e.target===overlay) _finishAppDialog(APP_DIALOG.kind==='prompt'?null:false);
    });
  }
  if(cancelBtn) cancelBtn.addEventListener('click',()=>_finishAppDialog(APP_DIALOG.kind==='prompt'?null:false));
  if(closeBtn)  closeBtn.addEventListener('click',()=>_finishAppDialog(APP_DIALOG.kind==='prompt'?null:false));
  if(confirmBtn){
    confirmBtn.addEventListener('click',()=>{
      if(APP_DIALOG.kind==='prompt'){
        const input=$('appDialogInput');
        _finishAppDialog(input?input.value:null);
      }else{
        _finishAppDialog(true);
      }
    });
  }
  document.addEventListener('keydown',e=>{
    if(!_isAppDialogOpen()) return;
    if(e.key==='Escape'){
      e.preventDefault();
      _finishAppDialog(APP_DIALOG.kind==='prompt'?null:false);
      return;
    }
    if(e.key==='Enter'){
      if(window._isImeEnter&&window._isImeEnter(e)) return;
      const target=e.target;
      const isTextarea=target&&target.tagName==='TEXTAREA';
      if(!isTextarea){
        e.preventDefault();
        if(target===cancelBtn||target===closeBtn){
          _finishAppDialog(APP_DIALOG.kind==='prompt'?null:false);
        }else if(APP_DIALOG.kind==='prompt'){
          const input=$('appDialogInput');
          _finishAppDialog(input?input.value:null);
        }else{
          _finishAppDialog(true);
        }
      }
      return;
    }
    if(e.key==='Tab'){
      const nodes=_getAppDialogFocusable();
      if(!nodes.length) return;
      const idx=nodes.indexOf(document.activeElement);
      let nextIdx=idx;
      if(e.shiftKey){nextIdx=idx<=0?nodes.length-1:idx-1;}
      else{nextIdx=idx===-1||idx===nodes.length-1?0:idx+1;}
      e.preventDefault();
      nodes[nextIdx].focus();
    }
  }, true);
}

function showConfirmDialog(opts={}){
  _ensureAppDialogBindings();
  if(APP_DIALOG.resolve) _finishAppDialog(false,false);
  const overlay=$('appDialogOverlay'),dialog=$('appDialog'),title=$('appDialogTitle'),
    desc=$('appDialogDesc'),input=$('appDialogInput'),cancelBtn=$('appDialogCancel'),confirmBtn=$('appDialogConfirm');
  APP_DIALOG.resolve=null;APP_DIALOG.kind='confirm';APP_DIALOG.lastFocus=document.activeElement;
  if(title) title.textContent=opts.title||t('dialog_confirm_title');
  if(desc) desc.textContent=opts.message||'';
  if(input){input.style.display='none';input.value='';}
  if(cancelBtn) cancelBtn.textContent=opts.cancelLabel||t('cancel');
  if(confirmBtn){
    confirmBtn.textContent=opts.confirmLabel||t('dialog_confirm_btn');
    confirmBtn.classList.toggle('danger',!!opts.danger);
  }
  if(dialog) dialog.setAttribute('role',opts.danger?'alertdialog':'dialog');
  if(overlay){overlay.style.display='flex';overlay.setAttribute('aria-hidden','false');}
  return new Promise(resolve=>{
    APP_DIALOG.resolve=resolve;
    setTimeout(()=>((opts.focusCancel?cancelBtn:confirmBtn)||confirmBtn||cancelBtn).focus(),0);
  });
}

function showPromptDialog(opts={}){
  _ensureAppDialogBindings();
  if(APP_DIALOG.resolve) _finishAppDialog(null,false);
  const overlay=$('appDialogOverlay'),dialog=$('appDialog'),title=$('appDialogTitle'),
    desc=$('appDialogDesc'),input=$('appDialogInput'),cancelBtn=$('appDialogCancel'),confirmBtn=$('appDialogConfirm');
  APP_DIALOG.resolve=null;APP_DIALOG.kind='prompt';APP_DIALOG.lastFocus=document.activeElement;
  if(title) title.textContent=opts.title||t('dialog_prompt_title');
  if(desc) desc.textContent=opts.message||'';
  if(input){
    input.type=opts.inputType||'text';input.style.display='';
    // Pre-fill: prefer `value`, accept `defaultValue` as alias for callers that
    // mirror the standard HTMLInputElement.defaultValue naming. Both empty →
    // blank field (the default rename-from-scratch flow stays unchanged).
    const prefill=(opts.value!=null?opts.value:(opts.defaultValue!=null?opts.defaultValue:''));
    input.value=prefill;input.placeholder=opts.placeholder||'';
    input.autocomplete='off';input.spellcheck=false;
  }
  if(cancelBtn) cancelBtn.textContent=opts.cancelLabel||t('cancel');
  if(confirmBtn){confirmBtn.textContent=opts.confirmLabel||t('create');confirmBtn.classList.remove('danger');}
  if(dialog) dialog.setAttribute('role','dialog');
  if(overlay){overlay.style.display='flex';overlay.setAttribute('aria-hidden','false');}
  return new Promise(resolve=>{
    APP_DIALOG.resolve=resolve;
    setTimeout(()=>{
      if(input&&input.style.display!=='none'){
        input.focus();
        // Selection behavior on focus:
        //   selectStem:true → select everything before the LAST '.' (e.g. for
        //     'report.txt' selects 'report' so a user can retype the basename
        //     without losing the extension; matches macOS Finder rename UX).
        //     Falls back to selecting the full value when there's no '.' or
        //     the dot is at index 0 ('.gitignore' → full select).
        //   selectAll:true → select the entire prefilled value.
        //   default       → caret at end (current behavior).
        const v=input.value||'';
        if(opts.selectStem && v){
          const dot=v.lastIndexOf('.');
          if(dot>0) input.setSelectionRange(0,dot);
          else input.select();
        } else if(opts.selectAll && v){
          input.select();
        }
      } else if(confirmBtn) confirmBtn.focus();
    },0);
  });
}


function _copyText(text){
  if(navigator.clipboard && window.isSecureContext){
    return navigator.clipboard.writeText(text).catch(()=>{
      // Fallback if clipboard API fails (e.g. permissions)
      return _fallbackCopy(text);
    });
  }
  return _fallbackCopy(text);
}
function _fallbackCopy(text){
  return new Promise((resolve,reject)=>{
    const ta=document.createElement('textarea');
    ta.value=text;ta.style.cssText='position:fixed;left:0;top:0;width:2em;height:2em;padding:0;border:none;outline:none;box-shadow:none;background:transparent;z-index:-1';
    document.body.appendChild(ta);
    ta.focus();ta.select();
    try{document.execCommand('copy');resolve();}
    catch(e){reject(e);}
    finally{document.body.removeChild(ta);}
  });
}
function copyStatusSessionId(btn){
  const text=btn&&btn.getAttribute('data-copy-status-session');
  if(!text)return;
  _copyText(text).then(()=>{
    const orig=btn.innerHTML;
    btn.innerHTML=(typeof li==='function')?li('check',13):t('copied');
    btn.classList.add('copied');
    setTimeout(()=>{btn.innerHTML=orig;btn.classList.remove('copied');},1500);
  }).catch(()=>showToast(t('copy_failed')));
}
function copyMsg(btn){
  const row=btn.closest('[data-raw-text]');
  const text=row?row.dataset.rawText:'';
  if(!text)return;
  _copyText(text).then(()=>{
    const orig=btn.innerHTML;btn.innerHTML=li('check',13);btn.style.color='var(--blue)';
    setTimeout(()=>{btn.innerHTML=orig;btn.style.color='';},1500);
  }).catch(()=>showToast(t('copy_failed')));
}
function _copyThinkingText(btn){
  const card=btn&&btn.closest?btn.closest('.thinking-card'):null;
  if(!card)return;
  const pre=card.querySelector('.thinking-card-body pre');
  const text=pre?pre.textContent:'';
  if(!text)return;
  _copyText(text).then(()=>{
    const orig=btn.innerHTML;
    btn.innerHTML=li('check',12);
    btn.style.color='var(--accent)';
    setTimeout(()=>{btn.innerHTML=orig;btn.style.color='';},1500);
  }).catch(()=>showToast(t('copy_failed')));
}

// ── TTS: Text-to-Speech via Web Speech API (#499) ──
// Strips markdown, code blocks, and MEDIA: paths for clean speech output.
function _stripForTTS(text){
  // Remove code blocks entirely (```) — line-anchored to match #1438 fix
  text=text.replace(/(^|\n)[ ]{0,3}```(?:[\s\S]*?\n)?[ ]{0,3}```(?=\n|$)/g,' ');
  // Remove inline code
  text=text.replace(/`[^`]+`/g,' ');
  // Strip bold/italic
  text=text.replace(/\*\*(.+?)\*\*/g,'$1');
  text=text.replace(/\*(.+?)\*/g,'$1');
  text=text.replace(/__(.+?)__/g,'$1');
  text=text.replace(/_(.+?)_/g,'$1');
  // Strip headings
  text=text.replace(/^#{1,6}\s+/gm,'');
  // Strip links, keep text
  text=text.replace(/\[([^\]]+)\]\([^)]+\)/g,'$1');
  // Replace MEDIA: paths with a simple label
  text=text.replace(/MEDIA:[^\s]+/g,'a file');
  // Strip emoji and emoticons
  text=text.replace(/[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F1E0}-\u{1F1FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{FE00}-\u{FE0F}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FA6F}\u{1FA70}-\u{1FAFF}\u{200D}]/gu,'');
  // Strip HTML tags that may leak through markdown
  text=text.replace(/<[^>]+>/g,' ');
  // Collapse whitespace
  text=text.replace(/\s+/g,' ').trim();
  return text;
}

function _splitForTTS(text, maxChars){
  // Split long text into chunks at natural sentence/paragraph boundaries
  // to avoid browser SpeechSynthesis truncation on long texts.
  maxChars=maxChars||300;
  if(text.length<=maxChars) return [text];
  const chunks=[];
  let remaining=text;
  while(remaining.length>0){
    if(remaining.length<=maxChars){ chunks.push(remaining); break; }
    let splitAt=maxChars;
    const sentencePattern=new RegExp('^[\\s\\S]{0,'+(maxChars-1)+'}[。！？.!？](?=\\s|$)','g');
    const m=sentencePattern.exec(remaining);
    if(m) splitAt=m.index+m[0].length;
    else{
      const sub=remaining.slice(0,maxChars);
      const lastSpace=Math.max(sub.lastIndexOf(' '),sub.lastIndexOf('\n'),sub.lastIndexOf(','),sub.lastIndexOf('，'));
      if(lastSpace>maxChars*0.5) splitAt=lastSpace+1;
    }
    chunks.push(remaining.slice(0,splitAt).trim());
    remaining=remaining.slice(splitAt).trim();
  }
  return chunks.filter(Boolean);
}

let _ttsSpeaking=false;
let _ttsCurrentUtterance=null;
let _ttsChunkQueue=[];
let _ttsChunkIndex=0;
let _ttsActiveBtn=null;
let _playingEdgeAudio=null;

function _buildBrowserUtterance(text, btn){
  const utter=new SpeechSynthesisUtterance(text);
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
    _ttsChunkIndex++;
    if(_ttsChunkIndex<_ttsChunkQueue.length){
      const next=new SpeechSynthesisUtterance(_ttsChunkQueue[_ttsChunkIndex]);
      next.voice=utter.voice; next.rate=utter.rate; next.pitch=utter.pitch;
      next.onend=utter.onend; next.onerror=utter.onerror;
      _ttsCurrentUtterance=next;
      speechSynthesis.speak(next);
    } else {
      _ttsSpeaking=false; _ttsCurrentUtterance=null;
      _ttsChunkQueue=[]; _ttsChunkIndex=0; _ttsActiveBtn=null;
      if(btn) btn.dataset.speaking='0';
    }
  };
  utter.onerror=()=>{
    _ttsSpeaking=false; _ttsCurrentUtterance=null;
    _ttsChunkQueue=[]; _ttsChunkIndex=0; _ttsActiveBtn=null;
    if(btn) btn.dataset.speaking='0';
  };
  return utter;
}

function _playEdgeTtsChunked(text, btn){
  const chunks=_splitForTTS(text);
  const _playOne=function(idx){
    if(idx>=chunks.length){
      _ttsSpeaking=false;_playingEdgeAudio=null;
      if(btn) btn.dataset.speaking='0';
      return;
    }
    const chunk=chunks[idx];
    const voice=localStorage.getItem('hermes-tts-voice')||'zh-CN-XiaoxiaoNeural';
    const savedRate=parseFloat(localStorage.getItem('hermes-tts-rate'));
    const savedPitch=parseFloat(localStorage.getItem('hermes-tts-pitch'));
    let rate='', pitch='';
    if(!isNaN(savedRate)){const pct=Math.round((savedRate-1)*100);const sign=pct>=0?'+':'';rate=sign+pct+'%';}
    if(!isNaN(savedPitch)){const hz=Math.round((savedPitch-1)*50);const sign=hz>=0?'+':'';pitch=sign+hz+'Hz';}
    fetch(new URL('api/tts', document.baseURI || location.href).href, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:chunk, voice:voice, rate:rate, pitch:pitch})
    })
    .then(function(r){
      if(!r.ok){
        return r.json().catch(function(){return {};}).then(function(j){
          throw new Error((j&&j.error)||('TTS request failed: '+r.status));
        });
      }
      return r.blob();
    })
    .then(function(blob){
      if(!_ttsSpeaking) return;
      const url=URL.createObjectURL(blob);
      const audio=new Audio(url);
      _playingEdgeAudio=audio;
      audio.onended=function(){
        URL.revokeObjectURL(url);
        _playingEdgeAudio=null;
        if(_ttsSpeaking) _playOne(idx+1);
      };
      audio.onerror=function(){
        URL.revokeObjectURL(url);
        _playingEdgeAudio=null;
        _ttsSpeaking=false;
        if(btn) btn.dataset.speaking='0';
      };
      audio.play().catch(function(e){
        URL.revokeObjectURL(url);
        _playingEdgeAudio=null;
        _ttsSpeaking=false;
        if(btn) btn.dataset.speaking='0';
        if(typeof showToast==='function') showToast('Edge TTS error: '+(e&&e.message||e));
      });
    })
    .catch(function(e){
      _ttsSpeaking=false;_playingEdgeAudio=null;
      if(btn) btn.dataset.speaking='0';
      if(typeof showToast==='function') showToast('Edge TTS failed: '+(e&&e.message||e));
    });
  };
  _playOne(0);
}

function speakMessage(btn){
  if(btn&&btn.dataset.speaking==='1'){
    stopTTS();
    return;
  }
  stopTTS();

  const row=btn?btn.closest('[data-raw-text]'):null;
  const text=row?row.dataset.rawText:'';
  if(!text) return;

  const clean=_stripForTTS(text);
  if(!clean) return;

  const engine=localStorage.getItem('hermes-tts-engine')||'browser';
  if(engine==='edge'){
    _playEdgeTtsChunked(clean, btn);
    return;
  }

  if(!('speechSynthesis' in window)){
    showToast(t('tts_not_supported')||'Speech synthesis not supported in this browser.');
    return;
  }

  _ttsChunkQueue=_splitForTTS(clean);
  _ttsChunkIndex=0;
  _ttsActiveBtn=btn;
  _ttsSpeaking=true;
  if(btn) btn.dataset.speaking='1';

  const utter=_buildBrowserUtterance(_ttsChunkQueue[0], btn);
  _ttsCurrentUtterance=utter;
  speechSynthesis.speak(utter);
}

function stopTTS(){
  if('speechSynthesis' in window){
    speechSynthesis.cancel();
  }
  // Stop Edge TTS audio
  if(_playingEdgeAudio){
    try{ _playingEdgeAudio.pause(); _playingEdgeAudio.currentTime=0; }catch(_){}
    _playingEdgeAudio=null;
  }
  _ttsSpeaking=false;
  _ttsCurrentUtterance=null;
  _ttsChunkQueue=[];
  _ttsChunkIndex=0;
  _ttsActiveBtn=null;
  // Reset all speaking buttons
  document.querySelectorAll('[data-speaking="1"]').forEach(btn=>{ btn.dataset.speaking='0'; });
}

function autoReadLastAssistant(){
  const engine=localStorage.getItem('hermes-tts-engine')||'browser';
  if(engine==='browser'&&!('speechSynthesis' in window)) return;
  const pref=localStorage.getItem('hermes-tts-auto-read');
  if(pref!=='true') return;
  // Find the last assistant message segment in the DOM
  const rows=document.querySelectorAll('.msg-row[data-role="assistant"], .assistant-segment[data-raw-text]');
  if(!rows.length) return;
  const last=rows[rows.length-1];
  const text=last.dataset.rawText||'';
  if(!text.trim()) return;
  const clean=_stripForTTS(text);
  if(!clean) return;
  if(engine==='edge'){
    _playEdgeTtsChunked(clean, null);
    return;
  }
  // Use chunked playback for browser TTS
  _ttsChunkQueue=_splitForTTS(clean);
  _ttsChunkIndex=0;
  _ttsSpeaking=true;
  const utter=_buildBrowserUtterance(_ttsChunkQueue[0], null);
  _ttsCurrentUtterance=utter;
  speechSynthesis.speak(utter);
}

// ── Reconnect banner (B4/B5: reload resilience) ──
const INFLIGHT_KEY = 'hermes-webui-inflight'; // localStorage key for in-flight session tracking
const INFLIGHT_STATE_KEY = 'hermes-webui-inflight-state'; // localStorage snapshots for mid-stream reload recovery
const INFLIGHT_STATE_DEFAULT_LIMITS = {
  maxSessions:8,
  messages:24,
  toolCalls:48,
  stringChars:60000,
  jsonChars:1500000,
};

function _boundedInflightInt(value, fallback, min, max){
  const n=parseInt(value,10);
  if(!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}
function _getInflightStateLimits(){
  const configured=(typeof window!=='undefined'&&window._inflightStateLimits&&typeof window._inflightStateLimits==='object')?window._inflightStateLimits:{};
  return {
    maxSessions:_boundedInflightInt(configured.maxSessions, INFLIGHT_STATE_DEFAULT_LIMITS.maxSessions, 1, 25),
    messages:_boundedInflightInt(configured.messages, INFLIGHT_STATE_DEFAULT_LIMITS.messages, 1, 100),
    toolCalls:_boundedInflightInt(configured.toolCalls, INFLIGHT_STATE_DEFAULT_LIMITS.toolCalls, 1, 200),
    stringChars:_boundedInflightInt(configured.stringChars, INFLIGHT_STATE_DEFAULT_LIMITS.stringChars, 1000, 500000),
    jsonChars:_boundedInflightInt(configured.jsonChars, INFLIGHT_STATE_DEFAULT_LIMITS.jsonChars, 100000, 4000000),
  };
}

function _readInflightStateMap(){
  try{
    const raw=localStorage.getItem(INFLIGHT_STATE_KEY);
    const parsed=raw?JSON.parse(raw):{};
    return parsed&&typeof parsed==='object'?parsed:{};
  }catch(_){
    return {};
  }
}
function _isStorageQuotaError(err){
  return !!err && (
    err.name==='QuotaExceededError' ||
    err.name==='NS_ERROR_DOM_QUOTA_REACHED' ||
    err.code===22 ||
    err.code===1014
  );
}
function _truncateInflightValue(value, maxChars){
  const limits=_getInflightStateLimits();
  const stringLimit=_boundedInflightInt(maxChars, limits.stringChars, 1000, 500000);
  if(typeof value==='string'){
    if(value.length<=stringLimit) return value;
    return value.slice(0,stringLimit)+'\n\n[truncated for browser recovery storage]';
  }
  if(Array.isArray(value)) return value.map(v=>_truncateInflightValue(v, Math.max(2000, Math.floor(stringLimit/2))));
  if(value&&typeof value==='object'){
    const out={};
    for(const [k,v] of Object.entries(value)) out[k]=_truncateInflightValue(v, stringLimit);
    return out;
  }
  return value;
}
function _compactInflightState(state){
  const limits=_getInflightStateLimits();
  const messages=Array.isArray(state.messages)?state.messages.slice(-limits.messages):[];
  const toolCalls=Array.isArray(state.toolCalls)?state.toolCalls.slice(-limits.toolCalls):[];
  // Phase 2: persist the live todo snapshot so reload / SSE reattach
  // restores the panel without waiting for the next live `todo` write.
  // The list is bounded by the agent (typically <20 items) and each
  // item is small, so no per-list cap is needed beyond the existing
  // stringChars truncation in _truncateInflightValue.
  const todos=Array.isArray(state.todos)?state.todos:null;
  const todoStateMeta=(state.todoStateMeta&&typeof state.todoStateMeta==='object')?state.todoStateMeta:null;
  return _truncateInflightValue({
    streamId:state.streamId||null,
    messages,
    uploaded:Array.isArray(state.uploaded)?state.uploaded.slice(-20):[],
    toolCalls,
    todos,
    todoStateMeta,
  }, limits.stringChars);
}
function _writeInflightStateMap(all){
  const limits=_getInflightStateLimits();
  const entries=Object.entries(all||{})
    .sort((a,b)=>Number(b[1]&&b[1].updated_at||0)-Number(a[1]&&a[1].updated_at||0))
    .slice(0,limits.maxSessions);
  const compact={};
  for(const [sid,entry] of entries) compact[sid]=entry;
  let json=JSON.stringify(compact);
  if(json.length>limits.jsonChars){
    const current=entries[0];
    json=JSON.stringify(current?{[current[0]]:current[1]}:{});
  }
  if(json.length>limits.jsonChars){
    localStorage.removeItem(INFLIGHT_STATE_KEY);
    return false;
  }
  localStorage.setItem(INFLIGHT_STATE_KEY,json);
  return true;
}
function saveInflightState(sid, state){
  if(!sid||!state) return;
  const entry={..._compactInflightState(state),updated_at:Date.now()};
  try{
    const all=_readInflightStateMap();
    all[sid]=entry;
    _writeInflightStateMap(all);
  }catch(err){
    if(!_isStorageQuotaError(err)) return;
    try{
      localStorage.removeItem(INFLIGHT_STATE_KEY);
      _writeInflightStateMap({[sid]:entry});
    }catch(_){
      try{localStorage.removeItem(INFLIGHT_STATE_KEY);}catch(__){}
    }
  }
}
function loadInflightState(sid, streamId){
  if(!sid) return null;
  const all=_readInflightStateMap();
  const entry=all[sid];
  if(!entry) return null;
  if(streamId&&entry.streamId&&entry.streamId!==streamId) return null;
  if(entry.updated_at&&Date.now()-entry.updated_at>10*60*1000){
    clearInflightState(sid);
    return null;
  }
  return entry;
}
function clearInflightState(sid){
  if(!sid) return;
  try{
    const all=_readInflightStateMap();
    if(!(sid in all)) return;
    delete all[sid];
    if(Object.keys(all).length) localStorage.setItem(INFLIGHT_STATE_KEY, JSON.stringify(all));
    else localStorage.removeItem(INFLIGHT_STATE_KEY);
  }catch(_){ }
}

// ─── Todo state: single source of truth + render scheduling ─────────────────
//
// Three concerns live together so they can share state cleanly:
//
//   1. _todosHash(items)  — cheap content fingerprint; skips re-render when
//      a snapshot would paint the same DOM.  Used both as a short-circuit
//      and as the hash that compares "rendered vs current" snapshots.
//
//   2. scheduleTodosRefresh() — coalesces multiple `todo_state` events that
//      land in the same animation frame into a single loadTodos() call.
//      Skips work entirely when the panel is not active.
//
//   3. _hydrateTodosFromSession(session) — applies cold-load todo_state
//      from the session GET payload, or clears the panel when neither a
//      cold-load nor an INFLIGHT signal is available.  Called at every
//      `S.session = ...` settle point so cross-session navigation never
//      leaves a stale list visible.
//
// The hash is keyed on (id, content, status); the render itself uses
// `esc()` for any user-controlled string, so XSS surface is the same as
// any other innerHTML path in this file.
let _todosLastRenderedHash=null;
let _todosRenderRafId=0;

function _todosHash(items){
  if(!Array.isArray(items)) return '';
  // String concat outperforms JSON.stringify on small arrays in V8 (no
  // intermediate object allocation) and is exact enough — the field set
  // matches what the renderer reads, so any visible change in DOM
  // implies a hash change.  Field separators (\x1f, \x1e) are control
  // chars unlikely to appear in real todo content, so collisions across
  // boundaries are not realistic.
  let h=items.length+'|';
  for(let i=0;i<items.length;i++){
    const t=items[i]||{};
    h+=String(t.id==null?'':t.id)+'\x1f'+String(t.content==null?'':t.content)+'\x1f'+String(t.status==null?'':t.status)+'\x1e';
  }
  return h;
}

function _todosPanelIsActive(){
  if(typeof document==='undefined') return false;
  const panel=document.getElementById('panelTodos');
  return !!(panel&&panel.classList&&panel.classList.contains('active'));
}

function scheduleTodosRefresh(){
  // Idempotent: many `todo_state` events fire on each tool result, but
  // only the latest snapshot needs to paint.  RAF lets us coalesce
  // without timer drift.
  if(_todosRenderRafId) return;
  if(typeof requestAnimationFrame!=='function'){
    if(typeof loadTodos==='function') loadTodos();
    return;
  }
  _todosRenderRafId=requestAnimationFrame(()=>{
    _todosRenderRafId=0;
    if(!_todosPanelIsActive()) return;
    if(typeof loadTodos==='function') loadTodos();
  });
}

function _resetTodosRenderCache(){
  // Clear after every cross-session navigation so the next render is
  // never short-circuited against a hash from a different session.
  _todosLastRenderedHash=null;
}

function _hydrateTodosFromSession(session){
  // Three input cases, three deterministic outcomes:
  //   a) cold-load AND inflight both present  → pick newer by ts so a
  //      stale cold-load from the session GET cannot regress a fresher
  //      INFLIGHT snapshot persisted from a still-running stream
  //      (avoids visible rollback on reload).
  //   b) only one of cold-load / inflight is present  → use it.
  //   c) neither  → reset to empty + sentinel so loadTodos() falls
  //      through to the legacy reverse-scan or paints the empty state.
  const sid=(session&&session.session_id)||'';
  const inflight=(typeof INFLIGHT==='object'&&INFLIGHT&&sid)?INFLIGHT[sid]:null;
  const cold=session&&session.todo_state;
  const coldOk=!!(cold&&Array.isArray(cold.todos));
  const inflightOk=!!(inflight&&Array.isArray(inflight.todos)&&inflight.todoStateMeta);
  const coldTs=coldOk?(Number(cold.ts)||0):0;
  const inflightTs=inflightOk?(Number(inflight.todoStateMeta&&inflight.todoStateMeta.ts)||0):0;
  // Whether a live stream currently owns this session. This is the signal
  // that disambiguates a ts-less cold-load (see below); it comes from the
  // session GET payload (mirrors sessions.js `S.session.active_stream_id`).
  const streamActive=!!(session&&session.active_stream_id);
  if(coldOk&&inflightOk){
    // Reconcile the server's settled cold-load snapshot against the
    // locally-persisted INFLIGHT snapshot.
    //
    // coldTs===0 means the cold-load carries NO usable timestamp, so we
    // cannot order it against INFLIGHT by recency. A todo tool message can
    // legitimately lose its `timestamp` during context compression/rebuild
    // (the on-disk message ends up timestamp=None), and derive_todo_state
    // (api/todo_state.py) then returns the correct latest-by-POSITION todos
    // but omits `ts`. The tie-break depends on who owns the INFLIGHT tail:
    //
    //   - stream ACTIVE → INFLIGHT is the live tail. The most recent todo
    //     write may still be in flight and not yet settled into the message
    //     list derive_todo_state scans, so a ts-less cold-load can be an
    //     OLDER (pre-latest-write) view. Letting cold win here rolls the
    //     panel back to a stale list, and since the stream may have just
    //     ended on that very write there is no guaranteed forward SSE event
    //     to self-heal. So prefer INFLIGHT. If cold is in fact newer, the
    //     reattach replay (sessions.js attachLiveStream, reconnecting) re-
    //     emits the journaled `todo_state` events which reconcile forward by
    //     ts, so any transient discrepancy corrects itself.
    //
    //   - stream IDLE → INFLIGHT is leftover from a finished/crashed stream
    //     (idle sessions purge it shortly after, sessions.js), and there is
    //     no replay to correct anything. The settled cold-load is the
    //     authoritative latest-by-position view, so prefer cold. This also
    //     preserves the original fix for the "shows an old todo list" bug,
    //     where a stale prior-turn INFLIGHT must not beat a ts-less cold-load.
    //
    // When coldTs>0 the original recency rule stands: strict ">", and on a
    // tie prefer INFLIGHT for the freshest in-tab edits.
    const coldWins=(coldTs===0)?(!streamActive):(coldTs>inflightTs);
    if(coldWins){
      S.todos=cold.todos;
      S.todoStateMeta={
        ts:coldTs,
        source:'cold-load',
        version:Number(cold.version)||1,
      };
    }else{
      S.todos=inflight.todos;
      S.todoStateMeta=inflight.todoStateMeta;
    }
  }else if(coldOk){
    S.todos=cold.todos;
    S.todoStateMeta={
      ts:coldTs,
      source:'cold-load',
      version:Number(cold.version)||1,
    };
  }else if(inflightOk){
    S.todos=inflight.todos;
    S.todoStateMeta=inflight.todoStateMeta;
  }else{
    S.todos=[];
    S.todoStateMeta=null;
  }
  _resetTodosRenderCache();
}

function snapshotLiveTurnHtmlForSession(sid){
  // Keep the DOM snapshot memory-only. Persisted INFLIGHT state intentionally
  // stores structured stream state, not outerHTML, so a hard reload still uses
  // the safer flat replay path instead of reviving stale nodes/listeners.
  if(!sid||!INFLIGHT[sid]) return;
  const turn=$('liveAssistantTurn');
  if(!turn) return;
  if(turn.dataset&&turn.dataset.sessionId&&turn.dataset.sessionId!==sid) return;
  INFLIGHT[sid].liveTurnHtml=turn.outerHTML;
}

function _liveAssistantSegmentTextLength(seg){
  if(!seg) return 0;
  const body=seg.querySelector('.msg-body')||seg;
  return String(body.textContent||'').trim().length;
}

function _mergeRestoredLiveAssistantSegment(restored, existing){
  if(!restored||!existing) return;
  const existingLive=existing.querySelector('[data-live-assistant="1"]');
  if(!existingLive) return;
  const restoredLive=restored.querySelector('[data-live-assistant="1"]');
  const existingLen=_liveAssistantSegmentTextLength(existingLive);
  const restoredLen=_liveAssistantSegmentTextLength(restoredLive);
  if(existingLen<=restoredLen) return;
  const replacement=existingLive.cloneNode(true);
  if(restoredLive){
    restoredLive.replaceWith(replacement);
    return;
  }
  const blocks=_assistantTurnBlocks(restored);
  if(!blocks) return;
  const anchor=Array.from(blocks.children).filter(el=>
    el.matches('.tool-call-group,.tool-card-row,.agent-activity-thinking,.thinking-card-row,[data-live-assistant="1"]')
  ).pop();
  if(anchor) anchor.insertAdjacentElement('afterend', replacement);
  else blocks.appendChild(replacement);
}

function restoreLiveTurnHtmlForSession(sid){
  const inflight=INFLIGHT[sid];
  if(!sid||!inflight||!inflight.liveTurnHtml) return false;
  const inner=$('msgInner');
  if(!inner) return false;
  const template=document.createElement('template');
  template.innerHTML=String(inflight.liveTurnHtml||'').trim();
  const restored=template.content.firstElementChild;
  if(!restored) return false;
  restored.id='liveAssistantTurn';
  if(S.session) restored.dataset.sessionId=S.session.session_id;
  const existing=$('liveAssistantTurn');
  _mergeRestoredLiveAssistantSegment(restored, existing);
  if(existing) existing.replaceWith(restored);
  else inner.appendChild(restored);
  const liveGroup=restored.querySelector('.tool-call-group[data-live-tool-call-group="1"]');
  if(liveGroup&&typeof _startActivityElapsedTimer==='function') _startActivityElapsedTimer(liveGroup);
  if(typeof placeLiveToolCardsHost==='function') placeLiveToolCardsHost();
  requestAnimationFrame(()=>postProcessRenderedMessages(restored));
  return true;
}

function markInflight(sid, streamId) {
  const payload=JSON.stringify({sid, streamId, ts: Date.now()});
  try{
    localStorage.setItem(INFLIGHT_KEY, payload);
  }catch(err){
    if(!_isStorageQuotaError(err)) return;
    try{
      localStorage.removeItem(INFLIGHT_STATE_KEY);
      localStorage.setItem(INFLIGHT_KEY, payload);
    }catch(_){}
  }
}
function clearInflight() {
  localStorage.removeItem(INFLIGHT_KEY);
}
function showReconnectBanner(msg) {
  $('reconnectMsg').textContent = msg || 'A response may have been in progress when you last left.';
  $('reconnectBanner').classList.add('visible');
}
function dismissReconnect() {
  $('reconnectBanner').classList.remove('visible');
  clearInflight();
}

// ── Live host resource health panel (#693) ──
const SYSTEM_HEALTH_INTERVAL_MS=5000;
let _systemHealthTimer=null;
function _systemHealthPercent(metric){
  const percent=Number(metric&&metric.percent);
  if(!Number.isFinite(percent)) return null;
  return Math.max(0,Math.min(100,Math.round(percent*10)/10));
}
function _formatSystemHealthPercent(percent){
  if(percent == null) return '—';
  return `${percent.toFixed(percent%1?1:0)}%`;
}
function _formatSystemHealthBytes(metric){
  if(!metric||!metric.used_bytes||!metric.total_bytes) return '';
  const units=['B','KB','MB','GB','TB'];
  const fmt=(bytes)=>{
    let value=Number(bytes)||0, idx=0;
    while(value>=1024&&idx<units.length-1){value/=1024;idx++;}
    return `${value.toFixed(value>=10||idx===0?0:1)} ${units[idx]}`;
  };
  return `${fmt(metric.used_bytes)} / ${fmt(metric.total_bytes)}`;
}
function _updateSystemHealthMetric(name,metric){
  const row=document.querySelector(`[data-system-health-metric="${name}"]`);
  if(!row) return;
  const rawPercent=_systemHealthPercent(metric);
  const percent=rawPercent == null ? 0 : rawPercent;
  const label=row.querySelector('[data-system-health-value]');
  const bar=row.querySelector('.system-health-bar');
  const fill=row.querySelector('.system-health-bar-fill');
  const text=_formatSystemHealthPercent(rawPercent);
  if(label){
    label.textContent=text;
    const bytes=(name==='memory'||name==='disk')?_formatSystemHealthBytes(metric):'';
    label.title=bytes||text;
  }
  if(bar) bar.setAttribute('aria-valuenow',String(percent));
  if(fill) fill.style.width=`${percent}%`;
}
function setSystemHealthUnavailable(message){
  const panel=$('systemHealthPanel');
  const status=$('systemHealthStatus');
  if(!panel) return;
  panel.classList.remove('loading');
  panel.classList.add('unavailable');
  if(status) status.textContent=message||'Unavailable';
  ['cpu','memory','disk'].forEach(name=>_updateSystemHealthMetric(name,null));
}
function renderSystemHealth(payload){
  const panel=$('systemHealthPanel');
  const status=$('systemHealthStatus');
  if(!panel) return;
  if(!payload||payload.available===false){
    setSystemHealthUnavailable('Unavailable');
    return;
  }
  panel.classList.remove('loading','unavailable');
  if(status) status.textContent=payload.status==='partial'?'Partial':'Live';
  _updateSystemHealthMetric('cpu',payload.cpu);
  _updateSystemHealthMetric('memory',payload.memory);
  _updateSystemHealthMetric('disk',payload.disk);
}
async function pollSystemHealth(){
  if(document.visibilityState !== 'visible') return;
  if(!_systemHealthPanelIsVisible()) return;
  try{
    const payload=await api('/api/system/health',{timeoutToast:false});
    renderSystemHealth(payload);
  }catch(_){
    setSystemHealthUnavailable('Unavailable');
  }
}
function _systemHealthPanelIsVisible(){
  return document.visibilityState === 'visible' &&
    !!document.querySelector('main.main.showing-insights') &&
    !!$('systemHealthPanel');
}
function startSystemHealthMonitor(){
  if(!_systemHealthPanelIsVisible()) return;
  if(_systemHealthTimer) return;
  void pollSystemHealth();
  _systemHealthTimer=setInterval(pollSystemHealth,SYSTEM_HEALTH_INTERVAL_MS);
}
function stopSystemHealthMonitor(){
  if(_systemHealthTimer){clearInterval(_systemHealthTimer);_systemHealthTimer=null;}
}
function _syncSystemHealthMonitorVisibility(){
  if(_systemHealthPanelIsVisible()) startSystemHealthMonitor();
  else stopSystemHealthMonitor();
}
document.addEventListener('visibilitychange',_syncSystemHealthMonitorVisibility);
if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',startSystemHealthMonitor);
else startSystemHealthMonitor();

// ── Hermes agent/gateway heartbeat alert (#716) ──
const AGENT_HEALTH_INTERVAL_MS=30000;
const AGENT_HEALTH_DISMISSED_KEY='agent-health-dismissed';
let _agentHealthTimer=null;
let _agentHealthLastState='unknown';
function _agentHealthDismissed(){
  try{return localStorage.getItem(AGENT_HEALTH_DISMISSED_KEY)==='1';}
  catch(_){return false;}
}
function _setAgentHealthDismissed(value){
  try{
    if(value)localStorage.setItem(AGENT_HEALTH_DISMISSED_KEY,'1');
    else localStorage.removeItem(AGENT_HEALTH_DISMISSED_KEY);
  }catch(_){ }
}
function _hideAgentHealthAlert(){
  const banner=$('agentHealthBanner');
  if(banner){banner.classList.remove('visible');banner.hidden=true;}
}
function _showAgentHealthAlert(payload){
  if(_agentHealthDismissed()) return;
  const banner=$('agentHealthBanner');
  const title=$('agentHealthTitle');
  const details=$('agentHealthDetails');
  if(!banner) return;
  if(title) title.textContent='Hermes agent is not responding';
  const state=payload&&payload.details&&payload.details.gateway_state?` State: ${payload.details.gateway_state}.`:'';
  if(details) details.textContent=`Gateway heartbeat failed.${state} Messages may not be delivered until it comes back.`;
  banner.hidden=false;
  banner.classList.add('visible');
}
function dismissAgentHealthAlert(){
  _setAgentHealthDismissed(true);
  _hideAgentHealthAlert();
}
async function pollAgentHealth(){
  if(document.visibilityState !== 'visible') return;
  try{
    const payload=await api('/api/health/agent',{timeoutToast:false});
    if(payload.alive === true){
      _agentHealthLastState='alive';
      _setAgentHealthDismissed(false);
      _hideAgentHealthAlert();
      return;
    }
    if(payload.alive === false){
      _agentHealthLastState='down';
      _showAgentHealthAlert(payload);
      return;
    }
    if(payload.alive == null){
      _agentHealthLastState='unknown';
      _hideAgentHealthAlert();
    }
  }catch(_){
    _agentHealthLastState='unknown';
    _hideAgentHealthAlert();
  }
}
function startAgentHealthMonitor(){
  if(document.visibilityState !== 'visible') return;
  if(_agentHealthTimer) return;
  void pollAgentHealth();
  _agentHealthTimer=setInterval(pollAgentHealth, AGENT_HEALTH_INTERVAL_MS);
}
function stopAgentHealthMonitor(){
  if(_agentHealthTimer){clearInterval(_agentHealthTimer);_agentHealthTimer=null;}
}
function _syncAgentHealthMonitorVisibility(){
  if(document.visibilityState === 'visible') startAgentHealthMonitor();
  else stopAgentHealthMonitor();
}
document.addEventListener('visibilitychange',_syncAgentHealthMonitorVisibility);
if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',startAgentHealthMonitor);
else startAgentHealthMonitor();
async function refreshSession() {
  // When the banner is in post-update restart mode, the "Reload" button
  // should do a full page reload — a session refresh would just 502 while
  // the server is still restarting.
  if (window._restartingForUpdate) { location.reload(); return; }
  dismissReconnect();
  if (!S.session) return;
  try {
    const data = await api(`/api/session?session_id=${encodeURIComponent(S.session.session_id)}`);
    S.session = data.session;
    S.messages = data.session.messages || [];
    const pendingMsg=getPendingSessionMessage(data.session,S.messages);
    if(pendingMsg) S.messages.push(pendingMsg);
    S.activeStreamId=data.session.active_stream_id||null;

    syncTopbar(); _renderMessagesWithScrollSnapshot();
    showToast('Conversation refreshed');
  } catch(e) { setStatus('Refresh failed: ' + e.message); }
}
// ── Update banner ──
function _formatUpdateTargetStatus(label,info){
  if(!info||!(info.behind>0)) return null;
  const release=(info.release_based&&info.latest_version)
    ?` (${info.current_version||'unknown'} -> ${info.latest_version})`
    :(info.branch?` (${info.branch})`:'');
  const noun=info.release_based?'release':'update';
  return `${label}${release}: ${info.behind} ${noun}${info.behind>1?'s':''}`;
}
function _formatUpdateCheckError(label,info){
  if(!info||!info.error) return null;
  const detail=String(info.error).replace(/^fetch failed:?\s*/i,'').trim();
  return detail ? `${label}: ${detail}` : label;
}
function _isSafeUpdateCompareUrl(url){
  if(!url||!/^https?:\/\//i.test(url)) return false;
  try{
    const parsed=new URL(url);
    return parsed.protocol==='https:'||parsed.protocol==='http:';
  }catch(e){
    return false;
  }
}
function _updateCompareUrl(info){
  if(!info) return null;
  const compareUrl=info.compare_url||null;
  if(compareUrl) return _isSafeUpdateCompareUrl(compareUrl)?compareUrl:null;
  const repo_url=info.repo_url;
  const currentSha=info.current_sha;
  const latestSha=info.latest_sha;
  if(!(repo_url&&currentSha&&latestSha)) return null;
  const fallbackUrl=repo_url+'/compare/'+currentSha+'...'+latestSha;
  return _isSafeUpdateCompareUrl(fallbackUrl)?fallbackUrl:null;
}
function _updateWhatsNewTargets(data){
  const targets=[
    {key:'webui',label:'WebUI',info:data&&data.webui},
    {key:'agent',label:'Agent',info:data&&data.agent},
  ];
  return targets.map((target)=>({
    key:target.key,
    label:target.label,
    info:target.info,
    url:_updateCompareUrl(target.info),
  })).filter((target)=>target.info&&target.info.behind>0&&target.url);
}
function _appendUpdateDiffLinks(container,targets,prefix){
  if(!container) return;
  if(prefix) container.appendChild(document.createTextNode(prefix));
  targets.forEach((target,idx)=>{
    if(idx>0) container.appendChild(document.createTextNode(' \u00b7 '));
    const link=document.createElement('a');
    link.href=target.url;
    link.target='_blank';
    link.rel='noopener';
    link.style.color='var(--accent)';
    link.style.textDecoration='underline';
    link.textContent=target.label;
    container.appendChild(link);
  });
}
function _hideUpdateSummaryPanel(){
  const panel=$('updateSummaryPanel');
  const text=$('updateSummaryText');
  const links=$('updateSummaryDiffLinks');
  if(panel) panel.style.display='none';
  if(text) text.textContent='';
  if(links){links.replaceChildren();links.style.display='none';}
}
const WHATS_NEW_SUMMARY_STORAGE_KEY='hermes-whats-new-generated-summaries';
function _loadStoredUpdateSummaries(){
  window._whatsNewGeneratedSummaries=window._whatsNewGeneratedSummaries||{};
  try{
    const raw=sessionStorage.getItem(WHATS_NEW_SUMMARY_STORAGE_KEY);
    if(!raw) return window._whatsNewGeneratedSummaries;
    const stored=JSON.parse(raw);
    if(stored&&typeof stored==='object') window._whatsNewGeneratedSummaries=stored;
  }catch(_e){
    try{sessionStorage.removeItem(WHATS_NEW_SUMMARY_STORAGE_KEY);}catch(_ignore){}
  }
  return window._whatsNewGeneratedSummaries;
}
function _persistGeneratedSummaries(){
  try{sessionStorage.setItem(WHATS_NEW_SUMMARY_STORAGE_KEY,JSON.stringify(window._whatsNewGeneratedSummaries||{}));}catch(_e){}
}
function _pruneGeneratedSummaries(data){
  const cache=_loadStoredUpdateSummaries();
  const valid=new Set(_updateWhatsNewTargets(data||{}).map((target)=>target.key));
  let changed=false;
  Object.keys(cache).forEach((key)=>{
    if(!valid.has(key)){delete cache[key];changed=true;}
  });
  if(changed) _persistGeneratedSummaries();
}
function _updateSummarySignature(info){
  if(!info) return '';
  return [info.current_sha||'',info.latest_sha||'',info.behind||0,info.compare_url||''].join('|');
}
function _updateSummaryButtonLabel(target,data){
  const labels=target.key==='webui'
    ? {generate:'Generate WebUI update summary',view:'View generated WebUI update summary',regenerate:'Re-generate WebUI update summary'}
    : {generate:'Generate Agent update summary',view:'View generated Agent update summary',regenerate:'Re-generate Agent update summary'};
  const cache=_loadStoredUpdateSummaries()[target.key];
  const signature=_updateSummarySignature(data&&data[target.key]);
  if(cache&&cache.signature===signature&&cache.payload) return labels.view;
  if(cache&&cache.signature!==signature) return labels.regenerate;
  return labels.generate;
}
function _rememberGeneratedSummary(target,payload,data){
  if(!target) return;
  window._whatsNewGeneratedSummaries=window._whatsNewGeneratedSummaries||{};
  window._whatsNewGeneratedSummaries[target]={
    signature:_updateSummarySignature(data&&data[target]),
    payload:payload,
  };
  _persistGeneratedSummaries();
}
function _renderUpdateSummaryPanel(payload,data,targetKey){
  const panel=$('updateSummaryPanel');
  const text=$('updateSummaryText');
  const links=$('updateSummaryDiffLinks');
  if(!panel||!text) return;
  panel.style.display='block';
  const sections=Array.isArray(payload&&payload.summary_sections)?payload.summary_sections:null;
  text.replaceChildren();
  if(sections&&sections.length){
    const wrap=document.createElement('div');
    wrap.id='updateSummarySections';
    wrap.style.display='grid';
    wrap.style.gap='8px';
    sections.forEach((section)=>{
      const block=document.createElement('section');
      const title=document.createElement('div');
      title.style.fontWeight='650';
      title.style.marginBottom='3px';
      title.textContent=section.title||'Summary';
      block.appendChild(title);
      const ul=document.createElement('ul');
      ul.style.margin='0';
      ul.style.paddingLeft='18px';
      (Array.isArray(section.items)?section.items:[]).forEach((item)=>{
        const li=document.createElement('li');
        li.textContent=String(item||'').trim();
        if(li.textContent) ul.appendChild(li);
      });
      if(!ul.children.length){
        const li=document.createElement('li');
        li.textContent='No summary details available.';
        ul.appendChild(li);
      }
      block.appendChild(ul);
      wrap.appendChild(block);
    });
    text.appendChild(wrap);
  }else{
    text.textContent=(payload&&payload.summary)||payload||'No summary available.';
  }
  const targets=_updateWhatsNewTargets(data||window._updateData||{}).filter((target)=>!targetKey||target.key===targetKey);
  if(links){
    links.replaceChildren();
    if(targets.length){
      links.style.display='block';
      _appendUpdateDiffLinks(links,targets,'Regular diff comparison: ');
    }else{
      links.style.display='none';
    }
  }
}
async function showWhatsNewSummary(target){
  const data=window._updateData||{};
  const scopedUpdates=target?{[target]:data[target]}:data;
  const cache=target?_loadStoredUpdateSummaries()[target]:null;
  const signature=target?_updateSummarySignature(data[target]):'';
  if(cache&&cache.signature===signature&&cache.payload){
    _renderUpdateSummaryPanel(cache.payload,data,target);
    _renderUpdateWhatsNewLinks(data,{mode:'summary'});
    return;
  }
  _renderUpdateSummaryPanel({summary:'Writing a simple summary…'},data,target);
  try{
    const res=await api('/api/updates/summary',{method:'POST',body:JSON.stringify({updates:scopedUpdates,target:target||null}),timeoutMs:60000});
    _rememberGeneratedSummary(target,res,data);
    _renderUpdateSummaryPanel(res,data,target);
    _renderUpdateWhatsNewLinks(data,{mode:'summary'});
  }catch(e){
    console.warn('[updates] summary failed',e);
    _renderUpdateSummaryPanel({
      summary_sections:[
        {title:"What you'll notice",items:['Could not generate the summary right now.']},
        {title:'Worth knowing',items:['Try again later, or use the comparison links below for the raw update details.']},
      ],
    },data,target);
  }
}
function _renderUpdateWhatsNewLinks(data){
  const options=arguments.length>1&&arguments[1]?arguments[1]:{};
  const container=$('updateWhatsNewLinks');
  if(!container) return;
  container.replaceChildren();
  const targets=_updateWhatsNewTargets(data);
  if(!targets.length){
    container.style.display='none';
    _hideUpdateSummaryPanel();
    return;
  }
  container.style.display='block';
  _pruneGeneratedSummaries(data);
  const useSummary=(options.mode||'')==='summary'||window._whatsNewSummaryEnabled===true;
  if(useSummary){
    targets.forEach((target,idx)=>{
      if(idx>0) container.appendChild(document.createTextNode(' \u00b7 '));
      const btn=document.createElement('button');
      btn.type='button';
      btn.className='linklike';
      btn.style.color='var(--accent)';
      btn.style.textDecoration='underline';
      btn.style.background='none';
      btn.style.border='0';
      btn.style.padding='0';
      btn.style.cursor='pointer';
      btn.textContent=_updateSummaryButtonLabel(target,data);
      btn.onclick=()=>showWhatsNewSummary(target.key);
      container.appendChild(btn);
    });
    return;
  }
  _hideUpdateSummaryPanel();
  if(targets.length===1){
    const target=targets[0];
    const link=document.createElement('a');
    link.href=target.url;
    link.target='_blank';
    link.rel='noopener';
    link.style.color='var(--accent)';
    link.style.textDecoration='underline';
    link.textContent="What's new in "+target.label+'?';
    container.appendChild(link);
    return;
  }
  _appendUpdateDiffLinks(container,targets,"What's new: ");
}
function _showUpdateBanner(data){
  const parts=[];
  const webuiPart=_formatUpdateTargetStatus('WebUI',data.webui);
  const agentPart=_formatUpdateTargetStatus('Agent',data.agent);
  if(webuiPart) parts.push(webuiPart);
  if(agentPart) parts.push(agentPart);
  window._updateData=data;
  if(!parts.length){
    _renderUpdateWhatsNewLinks(data);
    const staleBanner=$('updateBanner');
    if(staleBanner) staleBanner.classList.remove('visible');
    return;
  }
  const msg=$('updateMsg');
  if(msg) msg.textContent='\u2B06 '+parts.join(', ')+' available';
  const banner=$('updateBanner');
  if(banner) banner.classList.add('visible');
  const summaryMode=window._whatsNewSummaryEnabled===true?'summary':'diff';
  _renderUpdateWhatsNewLinks(data,{mode:summaryMode});
}
function dismissUpdate(){
  const b=$('updateBanner');if(b)b.classList.remove('visible');
  sessionStorage.setItem('hermes-update-dismissed','1');
}
function _isUpdateApplyNetworkError(error){
  if(error && error.status) return false;
  const message=(error&&error.message)||String(error||'');
  return /Failed to fetch|NetworkError|Load failed/i.test(message);
}
function _formatUpdateApplyExceptionMessage(error){
  if(_isUpdateApplyNetworkError(error)){
    return 'Update failed: could not reach the WebUI server. It may have restarted or the connection was interrupted. Please wait a few seconds, reload the page, then check the server if it still does not come back.';
  }
  const message=(error&&error.message)||String(error||'unknown error');
  return 'Update failed: '+message;
}
async function applyUpdates(){
  if(window._updateApplyInFlight) return;
  window._updateApplyInFlight=true;
  const btn=$('btnApplyUpdate');
  const resetApplyButton=(delayMs)=>{
    const reset=()=>{
      window._updateApplyInFlight=false;
      if(btn){btn.disabled=false;btn.textContent='Update Now';}
    };
    if(delayMs>0) setTimeout(reset,delayMs);
    else reset();
  };
  if(btn){btn.disabled=true;btn.textContent='Updating\u2026';}
  const errEl=$('updateError');
  if(errEl){errEl.style.display='none';errEl.textContent='';}
  // Hide any leftover force-update button from a prior conflict so a fresh
  // retry starts clean (otherwise stale state points at the wrong target).
  const forceBtnReset=$('btnForceUpdate');
  if(forceBtnReset){forceBtnReset.style.display='none';forceBtnReset.dataset.target='';}
  const targets=[];
  if(window._updateData?.webui?.behind>0) targets.push('webui');
  if(window._updateData?.agent?.behind>0) targets.push('agent');
  if(!targets.length){
    const msg='No update target selected. Refresh update status and retry.';
    if(errEl){errEl.textContent=msg;errEl.style.display='block';}
    else showToast(msg,5000,'error');
    resetApplyButton(0);
    return;
  }
  try{
    const stashConflictMessages=[];
    const baselineServerIdentity = await _readHealthServerIdentity();
    for(const target of targets){
      const res=await api('/api/updates/apply',{method:'POST',body:JSON.stringify({target}),timeoutMs:120000});
      if(!res.ok){
        _showUpdateError(target,res);
        resetApplyButton(0);
        return;
      }
      if(res.stash_conflict){
        stashConflictMessages.push('Update applied ('+target+'): '+(res.message||'Local changes were preserved in git stash.'));
        if(errEl){errEl.textContent=stashConflictMessages.join('\n\n');errEl.style.display='block';}
      }
    }
    const stashConflictMessage=stashConflictMessages.join('\n\n');
    showToast(stashConflictMessage||'Update applied — restarting…',stashConflictMessages.length?10000:undefined,stashConflictMessages.length?'warning':undefined);
    sessionStorage.removeItem('hermes-update-checked');
    sessionStorage.removeItem('hermes-update-dismissed');
    _waitForServerThenReload({baselineServerIdentity});
  }catch(e){
    const msg=_formatUpdateApplyExceptionMessage(e);
    if(errEl){errEl.textContent=msg;errEl.style.display='block';}
    else showToast(msg);
    resetApplyButton(_isUpdateApplyNetworkError(e)?5000:0);
  }
}
function _showUpdateError(target,res){
  const errEl=$('updateError');
  const forceBtn=$('btnForceUpdate');
  const msg='Update failed ('+target+'): '+(res.message||'unknown error');
  if(errEl){
    errEl.textContent=msg;
    errEl.style.display='block';
  } else {
    showToast(msg);
  }
  // Show "Force update" button when the error is recoverable by a hard reset
  if(forceBtn&&(res.conflict||res.diverged)){
    forceBtn.dataset.target=target;
    forceBtn.style.display='inline-block';
  }
}
function _normalizeHealthServerIdentity(rawIdentity){
  if(rawIdentity===undefined||rawIdentity===null) return null;
  if(typeof rawIdentity==='string'){
    const value=rawIdentity.trim();
    return value ? value : null;
  }
  const numeric=Number(rawIdentity);
  return Number.isFinite(numeric) ? String(numeric) : null;
}

async function _readHealthServerIdentity() {
  try {
    const r=await fetch(new URL('health', document.baseURI||location.href).href,{cache:'no-store'});
    if(!r.ok) return null;
    const data=await r.json();
    return _normalizeHealthServerIdentity(data&&data.server_started_at);
  } catch (_) {
    return null;
  }
}
async function forceUpdate(btn){
  const target=btn&&btn.dataset.target;
  if(!target) return;
  const confirmed=await showConfirmDialog({
    title:'Force update '+target+'?',
    message:'This will discard all local changes in the '+target+' repo and reset to the latest remote version. This cannot be undone.',
    confirmLabel:'Force update',
    danger:true,
    focusCancel:true,
  });
  if(!confirmed) return;
  btn.disabled=true;btn.textContent='Force updating\u2026';
  const errEl=$('updateError');
  if(errEl){errEl.style.display='none';}
  try{
    const baselineServerIdentity = await _readHealthServerIdentity();
    const res=await api('/api/updates/force',{method:'POST',body:JSON.stringify({target}),timeoutMs:120000});
    if(!res.ok){
      if(errEl){errEl.textContent='Force update failed: '+(res.message||'unknown error');errEl.style.display='block';}
      btn.disabled=false;btn.textContent='Force update';
      return;
    }
    showToast('Force update applied — restarting…');
    sessionStorage.removeItem('hermes-update-checked');
    sessionStorage.removeItem('hermes-update-dismissed');
    _waitForServerThenReload({baselineServerIdentity});
  }catch(e){
    if(errEl){errEl.textContent='Force update failed: '+e.message;errEl.style.display='block';}
    btn.disabled=false;btn.textContent='Force update';
  }
}

// Poll /health after an update-triggered restart, then reload.  Replaces the
// blind setTimeout(reload, 2500) that race-lost against slow hardware or
// reverse proxies that 502 immediately when the upstream socket closes (#874).
async function _waitForServerThenReload(opts){
  // Polls the /health endpoint; implementation uses a relative URL so subpath mounts keep working.
  opts=opts||{};
  const interval=opts.interval||500;
  const maxMs=opts.maxMs||15000;
  const baselineServerIdentity=_normalizeHealthServerIdentity(opts.baselineServerIdentity);
  window._restartingForUpdate=true;
  const msgEl=$('reconnectMsg');
  const banner=$('reconnectBanner');
  if(msgEl) msgEl.textContent='⏳ Restarting… please wait';
  if(banner) banner.classList.add('visible');
  const deadline=Date.now()+maxMs;
  // Give the server a moment to actually begin its restart before the first
  // probe — otherwise the old process may still respond ok on the first poll.
  await new Promise(r=>setTimeout(r, interval));
  while(Date.now()<deadline){
    try{
      const r=await fetch(new URL('health', document.baseURI||location.href).href,{cache:'no-store'});
      if(r.ok){
        let data={};
        try{ data=await r.json(); }catch(_){}
        if(data && data.status==='ok'){
          const nextServerIdentity=_normalizeHealthServerIdentity(data&&data.server_started_at);
          if (baselineServerIdentity===null){
            location.reload();
            return;
          }
          if (nextServerIdentity!==null && nextServerIdentity !== baselineServerIdentity){
            location.reload();
            return;
          }
          // Keep polling while the server keeps reporting the same (pre-restart) process identity
        }
      }
    }catch(_){ /* socket closed during restart — retry */ }
    await new Promise(r=>setTimeout(r, interval));
  }
  if(msgEl) msgEl.textContent='⚠️ Server is taking longer than expected — click Reload when ready';
}

function getPendingSessionMessage(session, messagesOverride=null){
  const text=String(session?.pending_user_message||'').trim();
  if(!text) return null;
  const attachments=Array.isArray(session?.pending_attachments)?session.pending_attachments.filter(Boolean):[];
  const sourceMessages=Array.isArray(messagesOverride)?messagesOverride:session?.messages;
  const messages=Array.isArray(sourceMessages)?sourceMessages:[];
  const lastUser=[...messages].reverse().find(m=>m&&m.role==='user');
  if(lastUser){
    const lastText=String(msgContent(lastUser)||'').trim();
    if(lastText===text){
      if(attachments.length&&!lastUser.attachments?.length) lastUser.attachments=attachments;
      return null;
    }
  }
  return {
    role:'user',
    content:text,
    attachments:attachments.length?attachments:undefined,
    _ts:session?.pending_started_at||Date.now()/1000,
    _pending:true,
  };
}
async function checkInflightOnBoot(sid) {
  const raw = localStorage.getItem(INFLIGHT_KEY);
  if (!raw) return;
  try {
    const {sid: inflightSid, streamId, ts} = JSON.parse(raw);
    if (inflightSid !== sid) { clearInflight(); return; }
    if (S.activeStreamId && S.activeStreamId === streamId) return;
    // Only show banner if the in-flight entry is less than 10 minutes old
    if (Date.now() - ts > 10 * 60 * 1000) { clearInflight(); return; }
    // Check if stream is still active
    const status = await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId || '')}`);
    if (status.active) {
      // Stream is genuinely still running -- show the banner
      showReconnectBanner(t('reconnect_active'));
    } else {
      // Stream finished. Only show banner if reload happened within 90 seconds
      // (longer gap = normal completed session, not a mid-stream reload)
      if (Date.now() - ts < 90 * 1000) {
        showReconnectBanner(t('reconnect_finished'));
      } else {
        clearInflight();  // completed normally, no banner needed
      }
    }
  } catch(e) { clearInflight(); }
}

function _topbarLoadedMessageCount(){
  const messages=Array.isArray(S.messages)?S.messages:[];
  return messages.filter(m=>m&&m.role&&m.role!=='tool').length;
}
function _topbarMessageMetaText(){
  const loadedCount=_topbarLoadedMessageCount();
  const totalCount=Number(S.session&&S.session.message_count);
  const hasTotal=Number.isFinite(totalCount)&&totalCount>0;
  const isTruncated=!!(typeof _messagesTruncated!=='undefined'&&_messagesTruncated);
  if(isTruncated&&hasTotal&&totalCount>loadedCount){
    return `${loadedCount} loaded of ${totalCount} messages`;
  }
  // Fully loaded: use the tool-row-filtered loadedCount, NOT the raw server
  // total (api/routes.py sets message_count to len(_all_msgs), which counts
  // role:"tool" rows the topbar has always excluded). Only the truncated
  // branch above surfaces the raw server total, and only as "loaded of total".
  return t('n_messages',loadedCount);
}
function syncTopbar(){
  if(!S.session){
    document.title=assistantDisplayName();
    if(typeof syncWorkspaceDisplays==='function') syncWorkspaceDisplays();
    if(typeof _syncWorkspaceHeadingState==='function') _syncWorkspaceHeadingState();
    if(typeof syncModelChip==='function') syncModelChip();
    if(typeof syncTerminalButton==='function') syncTerminalButton();
    if(typeof _syncHermesPanelSessionActions==='function') _syncHermesPanelSessionActions();
    else {
      const sidebarName=$('sidebarWsName');
      if(sidebarName && sidebarName.textContent==='Workspace'){
        sidebarName.textContent=t('no_workspace');
      }
    }
    if(typeof syncAppTitlebar==='function') syncAppTitlebar();
    // Update profile chip even when no session is active (e.g. right after profile switch)
    const _profileLabel=$('profileChipLabel');
    if(_profileLabel) _profileLabel.textContent=S.activeProfile||'default';
    return;
  }
  const sessionTitle=S.session.title||t('untitled');
  const _topbarTitle=$('topbarTitle');if(_topbarTitle)_topbarTitle.textContent=sessionTitle;
  document.title=sessionTitle+' \u2014 '+assistantDisplayName();
  const _topbarMeta=$('topbarMeta');
  if(_topbarMeta){
    let sourceLabel=(S.session&&(S.session.source_label||S.session.source_tag||S.session.raw_source))||'';
    // Recovered sidecars stamp source_label 'WebUI' (api/session_recovery.py); don't badge a native session as its own source (#3338).
    if(/^webui$/i.test(sourceLabel)) sourceLabel='';
    const metaText=_topbarMessageMetaText();
    _topbarMeta.textContent=metaText;
    if(sourceLabel){
      const badge=document.createElement('span');
      badge.className='topbar-source-badge';
      badge.textContent=sourceLabel+(S.session.read_only?' · read-only':'');
      _topbarMeta.appendChild(document.createTextNode(' '));
      _topbarMeta.appendChild(badge);
    }
  }
  if(typeof syncAppTitlebar==='function') syncAppTitlebar();
  if(typeof _syncWorkspaceHeadingState==='function') _syncWorkspaceHeadingState();
  // If a profile switch just happened, apply its model rather than the session's stale value.
  // S._pendingProfileModel is set by switchToProfile() and cleared here after one application.
  const modelOverride=S._pendingProfileModel;
  let currentModel=S.session.model||'';
  if(modelOverride){
    S._pendingProfileModel=null;
    const providerOverride=S._pendingProfileModelProvider||null;
    S._pendingProfileModelProvider=null;
    _applyModelToDropdown(modelOverride,$('modelSelect'),providerOverride);
    currentModel=modelOverride;
  } else {
    const modelSel=$('modelSelect');
    const rawCurrentModel=String(currentModel||'').trim();
    const hasSessionModel=rawCurrentModel&&rawCurrentModel.toLowerCase()!=='unknown';
    if(!hasSessionModel){
      // Missing/unknown session metadata must not leave the picker on the
      // previously viewed chat's model (#1771). Apply the configured default
      // first, then the first available option only as an HTML fallback.
      const fallback=_applySessionModelFallback(modelSel);
      if(fallback){
        // Defer state mutation + network write while the live model resolution
        // is in flight — sessions.js sets _modelResolutionDeferred=true between
        // the fast-path session render and the resolve_model=1 round-trip.
        // Persisting here would race that resolution and would also issue
        // silent /api/session/update POSTs against imported/read-only CLI
        // sessions whose model field reads "unknown" (#1779 stage-310 review).
        // The visible sel.value change still happens above for UX; only the
        // state mutation + persist defers.
        const deferModelCorrection=Boolean(S.session._modelResolutionDeferred);
        if(!deferModelCorrection){
          S.session.model=fallback.model;
          S.session.model_provider=fallback.model_provider||null;
          currentModel=fallback.model;
          _persistSessionModelCorrection(fallback.model,S.session.model_provider||null);
        }
      }
    } else {
      const applied=_applyModelToDropdown(currentModel,modelSel,S.session.model_provider||null);
      // If the session model is missing from the current provider list, inject
      // a session-scoped option instead of displaying the previous/static
      // selection. Only fall back if that repair path is unavailable.
      if(!applied){
        const deferModelCorrection=Boolean(S.session._modelResolutionDeferred);
        const missingModelIsRoutable=_providerDefersMissingModelFallback(S.session.model_provider||window._activeProvider||null);
        // Also defer if a live model fetch is still in flight — the model may be
        // in the list once the fetch completes. Persisting now would corrupt the
        // session with the wrong model before live models arrive (#1169).
        const liveStillPending=window._activeProvider&&_liveModelFetchPending.has(window._activeProvider);
        if(liveStillPending||missingModelIsRoutable){
          // Live fetch in flight — don't touch sel.value or S.session.model yet.
          // _addLiveModelsToSelect() will re-apply S.session.model once done (#1169).
          // Named custom providers/OpenRouter can also route vendor-prefixed IDs
          // outside the static catalog, so preserve the user's explicit choice.
          if(typeof _ensureModelOptionInDropdown==='function'){
            const sessionOption=_ensureModelOptionInDropdown(currentModel,modelSel,S.session.model_provider||null);
            if(sessionOption) currentModel=sessionOption;
          }
        } else {
          const sessionOption=(typeof _ensureModelOptionInDropdown==='function')
            ? _ensureModelOptionInDropdown(currentModel,modelSel,S.session.model_provider||null)
            : null;
          if(sessionOption){
            currentModel=sessionOption;
          } else {
            const fallback=_applySessionModelFallback(modelSel);
            if(fallback&&!deferModelCorrection){
              S.session.model=fallback.model;
              S.session.model_provider=fallback.model_provider||null;
              currentModel=fallback.model;
              // Persist the correction so the session doesn't re-inject on next load.
              _persistSessionModelCorrection(fallback.model,S.session.model_provider||null);
            }
          }
        }
      }
    }
  }
  if(typeof syncModelChip==='function') syncModelChip();
  if(typeof syncReasoningChip==='function') syncReasoningChip();
  if(typeof syncToolsetsChip==='function') syncToolsetsChip();
  // Show Clear button only when session has messages
  const clearBtn=$('btnClearConv');
  if(clearBtn) clearBtn.style.display=(S.messages&&S.messages.filter(msg=>msg.role!=='tool').length>0)?'':'none';
  if(typeof _syncHermesPanelSessionActions==='function') _syncHermesPanelSessionActions();
  if(typeof syncWorkspaceDisplays==='function') syncWorkspaceDisplays();
  if(typeof syncTerminalButton==='function') syncTerminalButton();
  // modelSelect already set above
  // Update profile chip label.
  // The chip is the profile-SWITCHER trigger (it fronts the profile dropdown) and
  // governs where the next message / new chat routes — both follow the client
  // active profile (the hermes_profile cookie, set only by /api/profile/switch).
  // It must therefore reflect S.activeProfile, NOT the loaded session's profile.
  // #3331 briefly keyed this on S.session.profile so the label would track the
  // session being browsed, but loadSession() never updates S.activeProfile, so
  // opening a cross-profile session made the chip disagree with the dropdown
  // checkmark and lie about message routing (#3635). #3331's legitimate work —
  // scoping project/session operations to the session's own profile — is
  // unaffected by this line.
  const profileLabel=$('profileChipLabel');
  if(profileLabel) profileLabel.textContent=S.activeProfile||'default';
}

function msgContent(m){
  // Extract plain text content from a message for filtering
  let c=m.content||'';
  if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('').trim();
  return String(c).trim();
}

function _isRecoveryControlMessageText(text){
  const normalized=String(text||'').replace(/\s+/g,' ').trim();
  if(!normalized) return false;
  const systemRecovery=/^\[System:/i.test(normalized)
    && /previous response was cut off by a network error/i.test(normalized)
    && /continue exactly where you left off/i.test(normalized);
  const backendRecovery=/^the live worker stopped before this run finished\.?$/i.test(normalized);
  return !!(systemRecovery || backendRecovery);
}
function _isRecoveryControlMessage(m){
  if(!m||m.role==='tool') return false;
  if(m.recovery_control===true) return true;
  // Backward-compat ONLY: strict fully-anchored text match for pre-marker
  // persisted sessions. NOT provider_details_label — a real "Response
  // interrupted" card carries 'Interruption details' and must stay visible.
  return _isRecoveryControlMessageText(msgContent(m)||String(m.content||''));
}
function _assistantMessageHasVisibleContent(m){
  if(!m||m.role!=='assistant') return false;
  if(_isRecoveryControlMessage(m)) return false;
  return !!msgContent(m);
}

function _fmtDateSep(d){
  const todayStart=new Date();todayStart.setHours(0,0,0,0);
  const dStart=new Date(d);dStart.setHours(0,0,0,0);
  const diffDays=Math.round((todayStart-dStart)/86400000);
  if(diffDays===0) return 'Today';
  if(diffDays===1) return 'Yesterday';
  if(diffDays>0 && diffDays<7) return dStart.toLocaleDateString([], {weekday:'long'});
  const opts={month:'short', day:'numeric'};
  if(todayStart.getFullYear()!==dStart.getFullYear()) opts.year='numeric';
  return dStart.toLocaleDateString([], opts);
}
const _ERR_MSG_RE=/^(?:\*\*error\b|error:|connection lost|no response received)/i;
function _messageHasReasoningPayload(m){
  if(!m||m.role!=='assistant') return false;
  if(m.reasoning) return true;
  if(Array.isArray(m.content)) return m.content.some(p=>p&&(p.type==='thinking'||p.type==='reasoning'));
  return /^\s*(?:<think>[\s\S]*?<\/think>|<\|channel\|?>thought\n?[\s\S]*?<channel\|>|<\|turn\|>thinking\n[\s\S]*?<turn\|>)/.test(String(m.content||''));
}
function _formatTurnTps(value){
  const n=Number(value);
  if(!Number.isFinite(n)||n<=0) return '';
  const fixed=n>=100?Math.round(n).toLocaleString():n>=10?n.toFixed(1):n.toFixed(1);
  return `${fixed} t/s`;
}
function isTpsDisplayEnabled(){
  return window._showTps===true;
}
function _assistantRoleHtml(tsTitle='', tpsText=''){
  const _bn=assistantDisplayName();
  const tps=(isTpsDisplayEnabled()&&tpsText)?`<span class="msg-tps-inline" title="Tokens per second">${esc(tpsText)}</span>`:'';
  return `<div class="msg-role assistant" ${tsTitle?`title="${esc(tsTitle)}"`:''}><div class="role-icon assistant">${esc(_bn.charAt(0).toUpperCase())}</div><span style="font-size:12px">${esc(_bn)}</span>${tps}</div>`;
}
function _setAssistantTurnTps(turn, tpsText=''){
  if(!turn) return;
  const role=turn.querySelector('.msg-role.assistant');
  if(!role) return;
  let chip=role.querySelector('.msg-tps-inline');
  const text=String(tpsText||'').trim();
  if(!text){if(chip) chip.remove();return;}
  if(!chip){
    chip=document.createElement('span');
    chip.className='msg-tps-inline';
    chip.title='Tokens per second';
    role.appendChild(chip);
  }
  chip.textContent=text;
}
function _setLiveAssistantTps(value){
  _setAssistantTurnTps($('liveAssistantTurn'), isTpsDisplayEnabled()?_formatTurnTps(value):'');
}
function _createAssistantTurn(tsTitle='', tpsText=''){
  const row=document.createElement('div');
  row.className='msg-row assistant-turn';
  row.dataset.role='assistant';
  if(S.session) row.dataset.sessionId=S.session.session_id;
  row.innerHTML=`${_assistantRoleHtml(tsTitle, tpsText)}<div class="assistant-turn-blocks"></div>`;
  return row;
}
function _assistantTurnBlocks(turn){
  return turn?turn.querySelector('.assistant-turn-blocks'):null;
}
function _thinkingCardHtml(text, open){
  const clean=_sanitizeThinkingDisplayText(text);
  const copyBtn=`<button class="thinking-copy-btn" onclick="event.stopPropagation();_copyThinkingText(this)" title="${t('copy')}" aria-label="${t('copy')}">${li('copy',12)}</button>`;
  const classes=`thinking-card${open?' open':''}`;
  return `<div class="${classes}"><div class="thinking-card-header" onclick="this.parentElement.classList.toggle('open')"><span class="thinking-card-icon">${li('lightbulb',14)}</span><span class="thinking-card-label">${t('thinking')}</span><span class="thinking-card-btn-row">${copyBtn}<span class="thinking-card-toggle">${li('chevron-right',12)}</span></span></div><div class="thinking-card-body"><pre>${esc(clean)}</pre></div></div>`;
}
function isSimplifiedToolCalling(){
  return window._simplifiedToolCalling!==false;
}
function _thinkingActivityNode(text, open){
  const row=document.createElement('div');
  row.className='agent-activity-thinking';
  row.innerHTML=_thinkingCardHtml(text, open);
  return row;
}
// ── Activity-group user expand intent (#1298) ──────────────────────────────
// When the user manually expands the live "Activity" dropdown during streaming,
// preserve that intent across the destroy/recreate cycle that fires on every
// thinking/tool event. Without this, ensureActivityGroup() re-creates the group
// with the default collapsed state and finalizeThinkingCard() force-collapses
// it whenever the assistant transitions from thinking → tool → thinking, so
// the panel snaps shut every few seconds while the user is trying to read it.
//
// The tracker is a singleton boolean: there is at most one live activity group
// at a time (selector .tool-call-group[data-live-tool-call-group="1"]). It is
// set to true when the user clicks the summary to expand, false when they
// click to collapse, and cleared back to undefined when the live group is
// finalized into a settled assistant turn (the live attribute is removed in
// _convertLiveActivityGroupToSettled / when liveAssistantTurn loses its id).
let _liveActivityUserExpanded;
const _activityDisclosureStoragePrefix='hermes-activity-disclosure:';
function _activityDisclosureStorageKey(activityKey){
  if(!activityKey||!S.session||!S.session.session_id) return null;
  return _activityDisclosureStoragePrefix+S.session.session_id+':'+activityKey;
}
function _readActivityDisclosureState(activityKey){
  const key=_activityDisclosureStorageKey(activityKey);
  if(!key) return null;
  try{
    const saved=localStorage.getItem(key);
    return saved==='open'||saved==='closed'?saved:null;
  }catch(_){return null;}
}
function _writeActivityDisclosureState(activityKey, open){
  const key=_activityDisclosureStorageKey(activityKey);
  if(!key) return;
  try{localStorage.setItem(key, open?'open':'closed');}catch(_){}
}
function _copyActivityDisclosureState(fromActivityKey, toActivityKey){
  const state=_readActivityDisclosureState(fromActivityKey);
  if(state) _writeActivityDisclosureState(toActivityKey, state==='open');
}
function _activityKeyForLiveTurn(){
  return S.activeStreamId?'live:'+S.activeStreamId:null;
}
function _onLiveActivityToggle(group){
  if(!group) return;
  // Only track explicit user clicks on the live group, not programmatic toggles.
  if(group.getAttribute('data-live-tool-call-group')!=='1') return;
  _liveActivityUserExpanded = !group.classList.contains('tool-call-group-collapsed');
}
function _toggleActivityGroup(summary){
  const group=summary&&summary.closest?summary.closest('.tool-call-group'):null;
  if(!group) return;
  const collapsed=group.classList.toggle('tool-call-group-collapsed');
  summary.setAttribute('aria-expanded',String(!collapsed));
  _writeActivityDisclosureState(group.getAttribute('data-activity-disclosure-key'), !collapsed);
  if(typeof _onLiveActivityToggle==='function') _onLiveActivityToggle(group);
}
function _clearLiveActivityUserIntent(){
  _liveActivityUserExpanded = undefined;
}
function ensureActivityGroup(inner, opts){
  opts=opts||{};
  if(!inner) return null;
  const live=!!opts.live;
  const activityKey=opts.activityKey||(live?_activityKeyForLiveTurn():null);
  const selector=live?'.tool-call-group[data-live-tool-call-group="1"][data-live-activity-current="1"]':'.tool-call-group[data-agent-activity-group="1"]';
  let group=inner.querySelector(selector);
  if(!group){
    group=document.createElement('div');
    let collapsed=opts.collapsed!==false;
    if(window._activityFeedExpandedDefault===true) collapsed=false;
    const savedState=_readActivityDisclosureState(activityKey);
    // Restore the user's explicit expand intent when recreating the live
    // activity group within the same turn (#1298), then let persisted chat/turn
    // state win across session switches and reloads. Saved closed-state should
    // override the default-expanded preference for settled groups the user has
    // explicitly collapsed.
    if(live && _liveActivityUserExpanded === true) collapsed=false;
    else if(live && _liveActivityUserExpanded === false) collapsed=true;
    if(savedState==='open') collapsed=false;
    else if(savedState==='closed') collapsed=true;
    group.className='tool-call-group agent-activity-group'+(collapsed?' tool-call-group-collapsed':'');
    group.setAttribute('data-tool-call-group','1');
    group.setAttribute('data-agent-activity-group','1');
    if(activityKey) group.setAttribute('data-activity-disclosure-key',activityKey);
    if(live){
      group.setAttribute('data-live-tool-call-group','1');
      group.setAttribute('data-live-activity-current','1');
    }
    group.innerHTML=`<button type="button" class="tool-call-group-summary" aria-expanded="${collapsed?'false':'true'}" onclick="_toggleActivityGroup(this)"><span class="tool-call-group-chevron">${li('chevron-right',12)}</span><span class="tool-call-group-label">Activity</span><span class="tool-call-group-duration"></span></button><div class="tool-call-group-body"></div>`;
    const anchor=opts.anchor||null;
    if(anchor&&anchor.parentElement===inner) anchor.insertAdjacentElement('afterend', group);
    else inner.appendChild(group);
  }else if(activityKey&&!group.getAttribute('data-activity-disclosure-key')){
    group.setAttribute('data-activity-disclosure-key',activityKey);
  }
  if(live){
    _setActivityElapsedStartedAt(group);
    _ensureLiveActivityBaseline(group);
  }
  _syncToolCallGroupSummary(group);
  if(live) _startActivityElapsedTimer(group);
  return group;
}
function closeCurrentLiveActivityGroup(){
  const turn=$('liveAssistantTurn');
  if(!turn) return;
  turn.querySelectorAll('.tool-call-group[data-live-tool-call-group="1"][data-live-activity-current="1"]').forEach(group=>{
    group.removeAttribute('data-live-activity-current');
  });
}
function _compressionStateForCurrentSession(){
  const state=window._compressionUi;
  if(!state||!S.session||state.sessionId!==S.session.session_id) return null;
  return state;
}
function isCompressionUiRunning(){
  const state=_compressionStateForCurrentSession();
  const lock=_compressionSessionLock();
  return !!((state&&state.phase==='running') || (lock && S.session && lock===S.session.session_id));
}
// Restore the composer placeholder saved when auto-compaction started. Safe to
// call whenever compression leaves the running state, from any path (clear,
// non-running setCompressionUi, or a direct window._compressionUi=null in the
// SSE handler) — it no-ops when nothing was saved. (#3512)
function _restoreCompressionPlaceholder(){
  const _input=$('msg');
  if(_input&&typeof _compressionPlaceholderSaved==='string'){
    _input.placeholder=_compressionPlaceholderSaved;
  }
  _compressionPlaceholderSaved=null;
}
function clearCompressionUi(){
  window._compressionUi=null;
  _clearCompressionElapsedTimer();
  _setCompressionSessionLock(null);
  _restoreCompressionPlaceholder();
  renderCompressionUi();
}
function setCompressionUi(state){
  if(!state){
    clearCompressionUi();
    return;
  }
  const nextState={...state};
  if(nextState.automatic&&nextState.phase==='running'&&!_compressionElapsedStartedAt(nextState)){
    nextState.startedAt=Date.now()/1000;
  }
  window._compressionUi=nextState;
  if(nextState.sessionId) _setCompressionSessionLock(nextState.sessionId);
  if(nextState.automatic&&nextState.phase==='running'){
    _startCompressionElapsedTimer();
    const _input=$('msg');
    if(_input&&_compressionPlaceholderSaved===null){
      _compressionPlaceholderSaved=_input.placeholder;
      _input.placeholder=typeof t==='function'?t('composer_compression_will_queue')||'Type a message — it will queue and send after compression':'Type a message — it will queue and send after compression';
    }
  } else {
    _clearCompressionElapsedTimer();
    // Leaving the running state (e.g. setCompressionUi(done)) must restore the
    // placeholder too — not only clearCompressionUi(). (#3512 leak fix)
    _restoreCompressionPlaceholder();
  }
  renderCompressionUi();
}
function _compressionCardsHtml(state){
  if(!state) return '';
  if(state.automatic) return _autoCompressionCardsHtml(state);
  const cmdText=state.commandText||'/compress';
  const focusText=state.focusTopic?`${t('focus_label')}: ${state.focusTopic}`:'';
  const headerText=state.phase==='done'
    ? (state.summary?.headline||t('compress_complete_label'))
    : state.phase==='error'
      ? (state.errorText||t('compress_failed_label'))
      : (typeof state.beforeCount==='number' ? t('n_messages', state.beforeCount) : '');
  const statusBody=state.phase==='error'
    ? [state.errorText||t('compress_failed_label'), focusText].filter(Boolean).join('\n')
    : [t('compressing'), focusText].filter(Boolean).join('\n');
  const statusLabel=state.phase==='done'
    ? t('compress_complete_label')
    : state.phase==='error'
      ? t('compress_failed_label')
      : t('compress_running_label');
  const statusIcon=state.phase==='done'
    ? li('check',13)
    : state.phase==='error'
      ? li('x',13)
    : `<span class="tool-card-running-dot"></span>`;
  const doneCardHtml=state.phase==='done'
    ? _compressionStatusCardHtml({
        statusLabel,
        previewText: headerText,
        detail: [state.summary?.token_line, state.summary?.note, focusText].filter(Boolean).join('\n'),
        icon: statusIcon,
        open: true,
        variantClass: 'tool-card-compress-complete',
      })
    : '';
  const referenceHtml=(state.phase==='done'&&state.referenceText)
    ? _compressionReferenceCardHtml(state.referenceText, false)
    : '';
  return `
    <div class="tool-card-row compression-card-row" data-compression-card="1">
      <div class="tool-card tool-card-compress-command">
        <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
          <span class="tool-card-icon">${li('settings',13)}</span>
          <span class="tool-card-name">${esc(t('command_label'))}</span>
          <span class="tool-card-preview">${esc(cmdText)}</span>
        </div>
      </div>
    </div>
    <div class="tool-card-row compression-card-row" data-compression-card="1">
      ${state.phase==='done'
        ? doneCardHtml
        : _compressionStatusCardHtml({
            statusLabel,
            previewText: headerText,
            detail: statusBody,
            icon: statusIcon,
            open: false,
            variantClass: state.phase==='error'
              ? 'tool-card-compress-error'
              : 'tool-card-compress-running',
          })
      }
    </div>
    ${referenceHtml}`;
}
function _autoCompressionBaseDetail(state){
  const fallback='Context auto-compressed to continue the conversation';
  const running=state&&state.phase==='running';
  return running
    ? (String(state.message||'Auto-compressing context...').trim()||'Auto-compressing context...')
    : (String(state&&state.message||fallback).trim()||fallback);
}
function _autoCompressionPreviewText(state){
  const copy=_engineAwareCompressionCopy(String(state&&state.engine||_compressionEngineForSession()).toLowerCase(), String(state&&state.mode||_compressionModeForSession()).toLowerCase());
  const running=state&&state.phase==='running';
  const detail=_autoCompressionBaseDetail(state);
  if(!running) return (String(state&&state.summary?.headline||copy.preview||detail).trim()||detail);
  const elapsedLabel=_compressionElapsedLabel(state);
  return [detail, elapsedLabel].filter(Boolean).join(' · ');
}
function _autoCompressionDetailText(state){
  const running=state&&state.phase==='running';
  const base=_autoCompressionBaseDetail(state);
  const elapsedLabel=running?_compressionElapsedLabel(state):'';
  if(running)return elapsedLabel?`Elapsed: ${elapsedLabel}`:base;
  const continuation=String(state&&state.continuationSessionId||'').trim();
  const handoff=continuation?`Continued in compressed session: ${continuation}`:'';
  return [base,handoff].filter(Boolean).join('\n');
}
function _autoCompressionCardsHtml(state){
  const copy=_engineAwareCompressionCopy(String(state&&state.engine||_compressionEngineForSession()).toLowerCase(), String(state&&state.mode||_compressionModeForSession()).toLowerCase());
  const running=state&&state.phase==='running';
  const preview=_autoCompressionPreviewText(state);
  const cardDetail=_autoCompressionDetailText(state);
  return `
    <div class="tool-card-row compression-card-row" data-compression-card="1">
      ${_compressionStatusCardHtml({
        statusLabel: (String(state&&state.engine||'').toLowerCase()==='lcm'||String(state&&state.mode||'').toLowerCase()==='lossless_retrieval')?copy.label:t('auto_compress_label'),
        previewText: preview,
        detail: cardDetail,
        icon: running ? '<span class="tool-card-running-dot"></span>' : li('check',13),
        open: running,
        variantClass: running
          ? 'tool-card-compress-running tool-card-compress-auto'
          : 'tool-card-compress-complete tool-card-compress-auto',
      })}
    </div>`;
}
function _compressionCardsNode(state){
  const wrap=document.createElement('div');
  wrap.className='compression-turn';
  wrap.innerHTML=`<div class="compression-turn-blocks">${_compressionCardsHtml(state)}</div>`;
  return wrap;
}
function appendLiveCompressionCard(state){
  if(!S.session||!S.activeStreamId||!state) return false;
  const scrollSnapshot=_captureMessageScrollSnapshot();
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    if(S.session) turn.dataset.sessionId=S.session.session_id;
    $('msgInner').appendChild(turn);
  }
  const inner=_assistantTurnBlocks(turn);
  if(!inner) return false;
  closeCurrentLiveActivityGroup();
  const node=_compressionCardsNode(state);
  if(!node) return false;
  node.setAttribute('data-live-compression-card','1');
  if(state.automatic&&state.phase==='running'){
    const started=_compressionElapsedStartedAt(state)||Date.now()/1000;
    node.setAttribute('data-compression-started-at',String(started));
    node.setAttribute('data-compression-message',String(state.message||'Auto-compressing context...'));
    _startCompressionElapsedTimer();
  } else {
    // Completion or error: clear the elapsed-timer attributes so the
    // interval reader (_compressionLiveCardState) doesn't keep treating
    // the replaced card as a running compression (#2973).
    node.removeAttribute('data-compression-started-at');
    node.removeAttribute('data-compression-message');
    // Only clear the global timer when the *active* session has no running
    // compression.  An SSE completion for a background session must not
    // kill the timer that's driving the current session's display.
    const _activeCompState = _compressionStateForCurrentSession();
    if (!_activeCompState || !_activeCompState.automatic || _activeCompState.phase !== 'running') {
      _clearCompressionElapsedTimer();
    }
  }
  const existing=inner.querySelector('[data-live-compression-card="1"]');
  if(existing) existing.replaceWith(node);
  else inner.appendChild(node);
  _restoreMessageScrollSnapshotSameFrame(scrollSnapshot);
  if(typeof scrollIfPinned==='function') scrollIfPinned();
  return true;
}
function _isHandoffSummaryToolPayload(value){
  if(!value||typeof value!=='object'||Array.isArray(value)) return false;
  return value._handoff_summary_card === true;
}
function _parseHandoffSummaryPayload(content){
  if(!content) return null;
  if(typeof content==='object' && !Array.isArray(content)) return _isHandoffSummaryToolPayload(content)?content:null;
  if(typeof content!=='string') return null;
  try {
    const parsed=JSON.parse(content);
    return _isHandoffSummaryToolPayload(parsed)?parsed:null;
  } catch (e) {
    return null;
  }
}
function _handoffSummaryStateFromMessage(m){
  if(!m||m.role!=='tool') return null;
  const payload = _parseHandoffSummaryPayload(m.content);
  if(!payload) return null;
  if(String(payload.session_id||'') && S.session && String(m.session_id||'') && String(payload.session_id)!==String(S.session.session_id||'')) {
    return null;
  }
  const summary = String(payload.summary||'').trim();
  if(!summary) return null;
  return {
    phase: 'done',
    channel: payload.channel || null,
    rounds: Number.isFinite(payload.rounds)?payload.rounds:null,
    summary,
    fallback: !!payload.fallback,
    generatedAt: Number(payload.generated_at) || null,
  };
}
function _collectHandoffSummaryStates(messages){
  const states=[];
  if(!Array.isArray(messages)) return states;
  for(let i=0;i<messages.length;i++){
    const state=_handoffSummaryStateFromMessage(messages[i]);
    if(state) states.push({state, rawIdx:i});
  }
  return states;
}
function _isContextCompactionMessage(m){
  if(!m||!m.role||m.role==='tool') return false;
  const text=msgContent(m)||String(m.content||'');
  return _isContextCompactionText(text);
}
function _isContextCompactionText(text){
  return /^\s*\[context compaction/i.test(String(text||'')) || /^\s*context compaction/i.test(String(text||''));
}
function _isPreservedCompressionTaskListMarkerText(text){
  return /^\s*\[your active task list was preserved across context compression\]/i.test(String(text||''));
}
function _isPreservedCompressionTaskListMarkerOnlyText(text){
  return _isPreservedCompressionTaskListMarkerText(text)
    && !String(text||'')
      .replace(/^\s*\[your active task list was preserved across context compression\]\s*/i,'')
      .trim();
}
function _isPreservedCompressionTaskListMessage(m){
  if(!m||m.role!=='user') return false;
  const text=msgContent(m)||String(m.content||'');
  return /^\s*\[your active task list was preserved across context compression\]/i.test(text);
}
function _isMarkerOnlyAssistantCompressionMessage(m){
  if(!m||m.role!=='assistant') return false;
  const text=msgContent(m)||String(m.content||'');
  return _isPreservedCompressionTaskListMarkerOnlyText(text);
}
function _preservedCompressionTaskListPreview(text){
  const body=String(text||'')
    .replace(/^\s*\[your active task list was preserved across context compression\]\s*/i,'')
    .trim();
  return (body.split(/\n+/).map(line=>line.trim()).filter(Boolean).slice(0,2).join(' ') || t('preserved_task_list_label'));
}
function _compressionMessageAnchorKey(m){
  if(!m||!m.role||m.role==='tool') return null;
  let content='';
  try{
    content=String(msgContent(m)||'');
  }catch(_){
    content=String(m.content||'');
  }
  const norm=content.replace(/\s+/g,' ').trim().slice(0,160);
  const ts=m._ts||m.timestamp||null;
  const attachments=Array.isArray(m.attachments)?m.attachments.length:0;
  if(!norm && !attachments && !ts) return null;
  return {role:String(m.role||''), ts, text:norm, attachments};
}
function _compressionAnchorIndex(visWithIdx, anchorKey, fallbackIdx=null){
  if(anchorKey&&Array.isArray(visWithIdx)){
    for(let i=visWithIdx.length-1;i>=0;i--){
      const candidate=_compressionMessageAnchorKey(visWithIdx[i].m);
      if(!candidate) continue;
      const anchorTs=String(anchorKey.ts??'');
      const candidateTs=String(candidate.ts??'');
      if(
        candidate.role===String(anchorKey.role||'') &&
        (!anchorTs||!candidateTs||candidateTs===anchorTs) &&
        String(candidate.text||'')===String(anchorKey.text||'') &&
        Number(candidate.attachments||0)===Number(anchorKey.attachments||0)
      ){
        return i;
      }
    }
  }
  return typeof fallbackIdx==='number' ? fallbackIdx : null;
}
function _latestCompressionReferenceMessage(messages, summaryText=''){
  if(!Array.isArray(messages)||!messages.length) return {message:null, rawIdx:-1};
  const summaryNorm=String(summaryText||'').replace(/\s+/g,' ').trim();
  for(let i=messages.length-1;i>=0;i--){
    const m=messages[i];
    if(!_isContextCompactionMessage(m)) continue;
    if(!summaryNorm) return {message:m, rawIdx:i};
    let content='';
    try{
      content=String(msgContent(m)||'');
    }catch(_){
      content=String((m&&m.content)||'');
    }
    const contentNorm=content.replace(/\s+/g,' ').trim();
    if(contentNorm.includes(summaryNorm)) return {message:m, rawIdx:i};
  }
  return {message:null, rawIdx:-1};
}
function _shouldShowSettledCompressionReference(referenceText){
  return !!String(referenceText||'').trim() && !_isContextCompactionText(referenceText);
}
function _compressionReferenceCardHtml(text, open=false){
  const copy=_engineAwareCompressionCopy();
  const preview=text.split(/\n+/).filter(Boolean).slice(0,2).join(' ');
  return `
    <div class="tool-card-row compression-card-row" data-compression-card="1" data-raw-text="${esc(text)}">
      <div class="tool-card tool-card-compress-reference${open?' open':''}">
        <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
          <span class="tool-card-icon">${li('star',13)}</span>
          <span class="tool-card-name">${esc(copy.label)}</span>
          <span class="tool-card-preview">${esc(copy.preview)} · ${esc(preview)}</span>
          <span class="tool-card-toggle">${li('chevron-right',12)}</span>
          <button class="msg-copy-btn msg-action-btn tool-card-copy compression-reference-copy" title="${t('copy')}" onclick="copyMsg(this);event.stopPropagation()">${li('copy',13)}</button>
        </div>
        <div class="tool-card-detail">
          <div class="tool-card-result">
          <pre>${esc(text)}</pre>
        </div>
        </div>
      </div>
      
    </div>`;
}
function _preservedCompressionTaskListCardHtml(m, open=false){
  const text=msgContent(m)||String(m.content||'');
  return `
    <div class="tool-card-row compression-card-row" data-compression-card="1" data-raw-text="${esc(text)}">
      ${_compressionStatusCardHtml({
        statusLabel: t('preserved_task_list_label'),
        previewText: _preservedCompressionTaskListPreview(text),
        detail: text,
        icon: li('list-todo',13),
        open,
        variantClass: 'tool-card-compress-reference',
      })}
    </div>`;
}
function _preservedCompressionTaskListCardsHtml(messages){
  return (messages||[]).map(m=>_preservedCompressionTaskListCardHtml(m, false)).join('');
}
function _latestTodoToolItems(messages){
  for(let i=(messages||[]).length-1;i>=0;i--){
    const m=messages[i];
    if(!m||m.role!=='tool') continue;
    try{
      const payload=typeof m.content==='string'?JSON.parse(m.content):m.content;
      if(payload&&Array.isArray(payload.todos)) return payload.todos;
    }catch(_){ }
  }
  return null;
}
function _hasActiveTodoItems(items){
  return Array.isArray(items) && items.some(item=>{
    const status=String(item&&item.status||'').trim().toLowerCase();
    return status==='pending'||status==='in_progress';
  });
}
function _latestPreservedCompressionTaskListMessages(messages){
  const latest=[...(messages||[])].reverse().find(m=>_isPreservedCompressionTaskListMessage(m));
  if(!latest) return [];
  const latestTodos=_latestTodoToolItems(messages);
  if(Array.isArray(latestTodos) && !_hasActiveTodoItems(latestTodos)) return [];
  return [latest];
}
function _isSameLocalDay(dateA, dateB){
  return dateA.getFullYear()===dateB.getFullYear()
    && dateA.getMonth()===dateB.getMonth()
    && dateA.getDate()===dateB.getDate();
}
function _formatMessageFooterTimestamp(tsVal){
  if(!tsVal) return '';
  const date=new Date(tsVal*1000);
  const now=new Date();
  // Use _formatInServerTz when available — it correctly handles fractional-hour
  // offsets like India +0530 that Etc/GMT cannot express. Falls back to plain
  // toLocaleString when sessions.js hasn't loaded yet.
  const fmt=(typeof _formatInServerTz==='function')?_formatInServerTz:null;
  if(_isSameLocalDay(date, now)){
    const opts={hour:'2-digit', minute:'2-digit'};
    return fmt?fmt(date,opts):date.toLocaleTimeString([], opts);
  }
  const opts={month:'short', day:'numeric', hour:'numeric', minute:'2-digit'};
  return fmt?fmt(date,opts):date.toLocaleString([], opts);
}
function _compressionEngineForSession(){
  return String(
    (S.session&&(
      S.session.compression_anchor_engine
      || S.session.context_engine
    )) || 'compressor'
  ).trim().toLowerCase() || 'compressor';
}
function _compressionModeForSession(){
  return String(
    (S.session&&S.session.compression_anchor_mode) || 'summary_compaction'
  ).trim().toLowerCase() || 'summary_compaction';
}
function _engineAwareCompressionCopy(engine=_compressionEngineForSession(), mode=_compressionModeForSession()){
  if(engine==='lcm'||mode==='lossless_retrieval'){
    return {
      label:t('retrieval_context_label'),
      preview:t('retrieval_context_preview'),
    };
  }
  return {
    label:t('context_compaction_label'),
    preview:t('reference_only_label'),
  };
}
function _compressionStatusCardHtml({
  statusLabel,
  previewText,
  detail,
  icon,
  open=false,
  variantClass='',
}){
  const statusDetail = String(detail || '').trim();
  const hasBody = !!statusDetail;
  const openClass = open ? ' open' : '';
  const statusIcon = icon;
  const bodyHtml = hasBody ? `<div class="tool-card-detail"><div class="tool-card-result"><pre>${esc(statusDetail)}</pre></div></div>` : '';
  const toggleHtml = hasBody ? `<span class="tool-card-toggle">${li('chevron-right',12)}</span>` : '';
  return `
    <div class="tool-card ${variantClass}${openClass}">
      <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
        ${statusIcon}
        <span class="tool-card-name">${esc(statusLabel)}</span>
        <span class="tool-card-preview">${esc(previewText)}</span>
        ${toggleHtml}
      </div>
      ${bodyHtml}
    </div>`;
}
function _handoffStateForCurrentSession(){
  const state=window._handoffUi;
  if(!state||!S.session||state.sessionId!==S.session.session_id) return null;
  return state;
}
function clearHandoffUi(){
  window._handoffUi=null;
  _renderMessagesWithScrollSnapshot();
}
function setHandoffUi(state){
  if(!state){
    clearHandoffUi();
    return;
  }
  window._handoffUi={...state};
  _renderMessagesWithScrollSnapshot();
}
function _handoffCardsHtml(state){
  if(!state) return '';
  const channel=String(state.channel||'').trim();
  const label=channel?`${channel} handoff summary`:'Handoff summary';
  const isError=state.phase==='error';
  const isDone=state.phase==='done';
  const isFallback=!!state.fallback;
  const detail=isError
    ? String(state.errorText||'Could not generate summary. Please try again.')
    : isDone
      ? String(state.summary||'')
      : 'Generating handoff summary...';
  const meta=typeof state.rounds==='number'
    ? `${state.rounds} external conversation rounds`
    : '';
  const icon=isError
    ? li('x',13)
    : isDone
      ? li('check',13)
      : '<span class="tool-card-running-dot"></span>';
  const bodyHtml=isDone&&!isError
    ? (
      `${renderMd(detail)}${
        isFallback
          ? '<p class="handoff-summary-fallback-note">Fallback summary generated from recent turns; no model-based rewrite was used.</p>'
          : ''
      }`
    )
    : `<p>${esc(detail)}</p>`;
  return `
    <div class="tool-card-row compression-card-row handoff-card-row" data-compression-card="1" data-handoff-card="1">
      <div class="tool-card tool-card-handoff-summary${isError?' tool-card-compress-error':''} open">
        <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
          ${icon}
          <span class="tool-card-name">${esc(label)}</span>
          ${meta?`<span class="tool-card-preview">${esc(meta)}</span>`:''}
          <span class="tool-card-toggle">${li('chevron-right',12)}</span>
        </div>
        <div class="tool-card-detail">
          <div class="tool-card-result handoff-summary-body">${bodyHtml}</div>
        </div>
      </div>
    </div>`;
}
function _handoffCardsNode(state){
  const wrap=document.createElement('div');
  wrap.className='compression-turn handoff-turn';
  wrap.innerHTML=`<div class="compression-turn-blocks">${_handoffCardsHtml(state)}</div>`;
  return wrap;
}
function _contextCompactionMessageHtml(m, tsTitle='', preservedMessages=[]){
  const text=msgContent(m)||String(m.content||'');
  return `<div class="compression-turn"><div class="compression-turn-blocks">${_compressionReferenceCardHtml(text, false, tsTitle)}${_preservedCompressionTaskListCardsHtml(preservedMessages)}</div></div>`;
}
function renderCompressionUi(){
  const el=$('liveCompressionCards');
  if(!el) return;
  el.innerHTML='';
  el.style.display='none';
}
// Session render cache: avoids full markdown+DOM rebuild when switching back
// to a session whose rendered transcript inputs are unchanged.
// Keyed by session_id. Only used on cross-session navigation, never for
// in-session updates (new messages, edits, stream events).
const _sessionHtmlCache=new Map();
let _sessionHtmlCacheSid=null; // session_id currently rendered in the DOM
function clearMessageRenderCache(){
  _sessionHtmlCache.clear();
  _sessionHtmlCacheSid=null;
  clearVisibleMessageRowCache();
}

function _messageRenderCacheSignature(){
  let hash=2166136261;
  function add(value){
    const s=String(value==null?'':value);
    for(let i=0;i<s.length;i++){
      hash^=s.charCodeAt(i);
      hash=Math.imul(hash,16777619)>>>0;
    }
    hash^=31;
    hash=Math.imul(hash,16777619)>>>0;
  }
  const messages=Array.isArray(S.messages)?S.messages:[];
  add(messages.length);
  for(const m of messages){
    if(!m||typeof m!=='object'){ add('missing'); continue; }
    add(m.role);add(m.timestamp);add(m._ts);add(m._error);add(m._statusCard);
    add(msgContent(m));
    if(Array.isArray(m.content)){
      add('content-array');
      m.content.forEach(part=>{
        if(!part||typeof part!=='object'){ add(part); return; }
        add(part.type);add(part.id);add(part.name);add(part.text);add(part.content);
      });
    }
    if(Array.isArray(m.tool_calls)){
      add('message-tool-calls');add(m.tool_calls.length);
      m.tool_calls.forEach(tc=>{add(tc&&tc.id);add(tc&&tc.name);add(tc&&tc.type);add(JSON.stringify(tc&&tc.function||{}));});
    }
    if(Array.isArray(m._partial_tool_calls)){
      add('partial-tool-calls');add(m._partial_tool_calls.length);
      m._partial_tool_calls.forEach(tc=>{add(tc&&tc.id);add(tc&&tc.name);add(tc&&tc.snippet);});
    }
    if(_messageHasReasoningPayload(m)) add(m.reasoning||m.thinking||m._reasoning||'reasoning');
    if(Array.isArray(m.attachments)) m.attachments.forEach(a=>add(a&&typeof a==='object'?JSON.stringify(a):a));
  }
  const toolCalls=Array.isArray(S.toolCalls)?S.toolCalls:[];
  add('settled-tool-calls');add(toolCalls.length);
  toolCalls.forEach(tc=>{
    if(!tc||typeof tc!=='object'){ add(tc); return; }
    add(tc.tid);add(tc.id);add(tc.name);add(tc.done);add(tc.is_diff);add(tc.assistant_msg_idx);add(tc.snippet);add(JSON.stringify(tc.args||{}));
  });
  if(S.session){
    add(S.session.message_count);add(S.session.updated_at);add(S.session.compression_anchor_visible_idx);
    add(JSON.stringify(S.session.compression_anchor_message_key||null));
    add(S.session.compression_anchor_summary||'');
  }
  return `${messages.length}:${toolCalls.length}:${hash.toString(16)}`;
}

function _clipCliToolSnippet(text, maxLen=20000){
  const s=String(text||'');
  if(s.length<=maxLen) return s;
  return `${s.slice(0,maxLen)}\n\n... truncated ${s.length-maxLen} chars ...`;
}

function _cliToolResultText(raw){
  const s=String(raw||'');
  try{
    const rd=JSON.parse(s);
    if(rd && typeof rd==='object'){
      for(const key of ['output','result','error','content','diff','patch']){
        if(Object.prototype.hasOwnProperty.call(rd,key)){
          const v=rd[key];
          if(v==null) return '';
          return typeof v==='string' ? v : JSON.stringify(v,null,2);
        }
      }
    }
  }catch(e){}
  return s;
}

function _cliLooksLikePatchDiff(text){
  const s=String(text||'');
  if(!s) return false;
  if(/\*\*\* Begin Patch/.test(s)) return true;
  if(/^diff --git /m.test(s)) return true;
  if(/^@@\s/m.test(s)) return true;
  if(/(^|\n)---\s+/.test(s) && /(^|\n)\+\+\+\s+/.test(s)) return true;
  return false;
}

function _cliToolResultSnippet(raw){
  const fullText=_cliToolResultText(raw);
  if(_cliLooksLikePatchDiff(fullText)) return _clipCliToolSnippet(fullText);
  return String(fullText||'').slice(0,4000);
}

function _prefixedCliDiffLines(prefix, value){
  return String(value||'').split('\n').map(line=>`${prefix}${line}`).join('\n');
}

function _firstOwnedValue(obj, keys){
  for(const key of keys){
    if(obj && Object.prototype.hasOwnProperty.call(obj,key)) return obj[key];
  }
  return undefined;
}

function _cliPatchSnippetFromArgs(name, args){
  if(!args || typeof args!=='object') return '';
  const toolName=String(name||'').toLowerCase();
  for(const key of ['patch','diff']){
    const v=args[key];
    if(typeof v==='string' && v.trim()) return _clipCliToolSnippet(v);
  }
  for(const key of ['input','content']){
    const v=args[key];
    if(typeof v==='string' && _cliLooksLikePatchDiff(v)) return _clipCliToolSnippet(v);
  }
  const isEditLike=toolName==='apply_patch'
    || toolName==='patch'
    || toolName.includes('edit')
    || toolName==='replace'
    || toolName==='str_replace';
  if(!isEditLike) return '';
  const oldValue=_firstOwnedValue(args,['old_string','old_str','old','before']);
  const newValue=_firstOwnedValue(args,['new_string','new_str','new','after']);
  if(oldValue!==undefined || newValue!==undefined){
    const path=String(_firstOwnedValue(args,['file_path','path','filename'])||'');
    const lines=[];
    if(path) lines.push(path);
    if(oldValue!==undefined) lines.push(_prefixedCliDiffLines('-', oldValue));
    if(newValue!==undefined) lines.push(_prefixedCliDiffLines('+', newValue));
    return _clipCliToolSnippet(lines.join('\n'));
  }
  if(Array.isArray(args.edits)){
    const path=String(_firstOwnedValue(args,['file_path','path','filename'])||'');
    const chunks=[];
    if(path) chunks.push(path);
    args.edits.slice(0,5).forEach(edit=>{
      if(!edit || typeof edit!=='object') return;
      const before=_firstOwnedValue(edit,['old_string','old_str','old','before']);
      const after=_firstOwnedValue(edit,['new_string','new_str','new','after']);
      if(before!==undefined) chunks.push(_prefixedCliDiffLines('-', before));
      if(after!==undefined) chunks.push(_prefixedCliDiffLines('+', after));
    });
    if(chunks.length) return _clipCliToolSnippet(chunks.join('\n'));
  }
  return '';
}

function _cliToolCardSnippet(resultSnippet, patchSnippet){
  if(_cliLooksLikePatchDiff(resultSnippet)) return resultSnippet;
  if(!patchSnippet) return resultSnippet || '';
  const result=String(resultSnippet||'').trim();
  if(!result) return patchSnippet;
  const generic=/^(success|ok|done|done\.|exit code: 0)$/i.test(result);
  if(generic) return patchSnippet;
  return `${resultSnippet}\n\n${patchSnippet}`;
}

function _cliToolCardHasDiffSnippet(resultSnippet, patchSnippet){
  return !!patchSnippet || _cliLooksLikePatchDiff(resultSnippet);
}

function _captureMessageScrollSnapshot(){
  const el=$('messages');
  if(!el) return null;
  const bottom=Math.max(0,el.scrollHeight-el.scrollTop-el.clientHeight);
  return {
    top:el.scrollTop,
    bottom,
    pinned:_shouldFollowMessagesOnDomReplace(),
    userUnpinned:_messageUserUnpinned,
  };
}
function _restoreMessageScrollSnapshot(snapshot){
  const el=$('messages');
  if(!el||!snapshot) return;
  const maxTop=Math.max(0,el.scrollHeight-el.clientHeight);
  _programmaticScroll=true;
  el.scrollTop=Math.max(0,Math.min(Number(snapshot.top)||0,maxTop));
  // Sync _lastScrollTop after programmatic restore so sticky-unpin does not false-trigger (#1731).
  _lastScrollTop=el.scrollTop;
  requestAnimationFrame(()=>{ setTimeout(()=>{_programmaticScroll=false;},0); });
}
function _restoreMessageScrollSnapshotSameFrame(snapshot){
  const el=$('messages');
  if(!el||!snapshot) return;
  const maxTop=Math.max(0,el.scrollHeight-el.clientHeight);
  const bottom=Number(snapshot.bottom);
  const target=(snapshot.pinned===true&&Number.isFinite(bottom))
    ? maxTop-Math.max(0,bottom)
    : Number(snapshot.top)||0;
  _programmaticScroll=true;
  el.scrollTop=Math.max(0,Math.min(target,maxTop));
  _lastScrollTop=el.scrollTop;
  if(snapshot.pinned===true){
    _messageUserUnpinned=false;
    _scrollPinned=true;
    _nearBottomCount=2;
  }else if(snapshot.userUnpinned===true){
    _messageUserUnpinned=true;
    _scrollPinned=false;
    _nearBottomCount=0;
  }
  requestAnimationFrame(()=>{ setTimeout(()=>{_programmaticScroll=false;},0); });
}
function _renderMessagesWithScrollSnapshot(options){
  const scrollSnapshot=_captureMessageScrollSnapshot();
  renderMessages({...(options||{}),preserveScroll:true});
  _restoreMessageScrollSnapshotSameFrame(scrollSnapshot);
}
function _scrollAfterMessageRender(preserveScroll, scrollSnapshot){
  // Terminal stream renders can happen after S.activeStreamId is cleared.
  // In that case, preserveScroll asks the normal pin-state helper to decide:
  // pinned users stay at bottom; users who manually scrolled up get their
  // pre-render scrollTop restored after the DOM replacement.
  if(preserveScroll){
    if(_scrollPinned) scrollIfPinned();
    else _restoreMessageScrollSnapshot(scrollSnapshot);
    return;
  }
  if(S.activeStreamId){
    scrollIfPinned();
    return;
  }
  scrollToBottom();
}

function renderMessages(options){
  const preserveScroll=!!(options&&options.preserveScroll);
  const scrollSnapshot=preserveScroll?_captureMessageScrollSnapshot():null;
  const inner=$('msgInner');
  const sid=S.session?S.session.session_id:null;
  const msgCount=S.messages.length;
  if(sid!==_messageRenderWindowSid) _resetMessageRenderWindow(sid);
  const renderWindowSize=_currentMessageRenderWindowSize();
  let cachedRenderSignature=null;
  const hasTransientTranscriptUi=!!(
    (window._compressionUi&&(!window._compressionUi.sessionId||window._compressionUi.sessionId===sid)) ||
    (window._handoffUi&&(!window._handoffUi.sessionId||window._handoffUi.sessionId===sid))
  );

  // Fast path: switching back to a previously rendered session with same count.
  // Guard: sid !== _sessionHtmlCacheSid ensures in-session updates (edits,
  // new messages, tool_complete) always get a fresh rebuild.
  // Skip cache if this session is still streaming — the live smd parser writes
  // into a DOM node inside the cached subtree; serving cached HTML detaches it.
  // Also skip cache for transient transcript cards such as /compress and
  // cross-channel handoff summaries; otherwise the cached transcript returns
  // before those cards can be inserted.
  if(sid&&sid!==_sessionHtmlCacheSid&&!INFLIGHT[sid]&&!hasTransientTranscriptUi){
    const renderSignature=_messageRenderCacheSignature();
    cachedRenderSignature=renderSignature;
    const cached=_sessionHtmlCache.get(sid);
    if(cached&&cached.msgCount===msgCount&&cached.renderWindowSize===renderWindowSize&&cached.signature===renderSignature){
      inner.innerHTML=cached.html;
      _sessionHtmlCacheSid=sid;
      _wireMessageWindowLoadEarlierButton();
      if(typeof _applySessionNavigationPrefs==='function') _applySessionNavigationPrefs();
      _scrollAfterMessageRender(preserveScroll, scrollSnapshot);
      requestAnimationFrame(()=>postProcessRenderedMessages(inner));
      if(typeof _initMediaPlaybackObserver==='function') _initMediaPlaybackObserver();
      if(typeof loadTodos==='function'&&document.getElementById('panelTodos')&&document.getElementById('panelTodos').classList.contains('active')){loadTodos();}
      return;
    }
  }

  const compressionState=_compressionStateForCurrentSession();
  if(window._compressionUi && !compressionState) clearCompressionUi();
  const handoffState=_handoffStateForCurrentSession();
  if(window._handoffUi && !handoffState) window._handoffUi=null;
  const sessionCompressionAnchor=(
    S.session && typeof S.session.compression_anchor_visible_idx==='number'
  ) ? S.session.compression_anchor_visible_idx : null;
  const sessionCompressionAnchorKey=(
    S.session && S.session.compression_anchor_message_key && typeof S.session.compression_anchor_message_key==='object'
  ) ? S.session.compression_anchor_message_key : null;
  const sessionCompressionSummary=(
    S.session && typeof S.session.compression_anchor_summary==='string'
  ) ? S.session.compression_anchor_summary.trim() : '';
  const preservedCompressionTaskMessages=_latestPreservedCompressionTaskListMessages(S.messages);
  const vis=S.messages.filter(m=>{
    if(!m||!m.role||m.role==='tool')return false;
    if(_isContextCompactionMessage(m)) return false;
    if(_isPreservedCompressionTaskListMessage(m)) return false;
    if(_isRecoveryControlMessage(m)) return false;
    if(m.role==='assistant'){
      const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
      const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
      const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
      if(hasTc||hasTu||hasPartialTc||_messageHasReasoningPayload(m)) return true;
      if(_assistantMessageHasVisibleContent(m)) return true;
    }
    return m._statusCard||msgContent(m)||m.attachments?.length;
  });
  $('emptyState').style.display=(vis.length||preservedCompressionTaskMessages.length)?'none':'';
  inner.innerHTML='';
  const compressionNode=compressionState?_compressionCardsNode(compressionState):null;
  const {message:referenceMessage, rawIdx:referenceMessageRawIdx}=_latestCompressionReferenceMessage(
    S.messages,
    sessionCompressionSummary
  );
  const referenceText=referenceMessage
    ? msgContent(referenceMessage)||String(referenceMessage.content||'')
    : sessionCompressionSummary;
  const referenceNode=(!compressionState && _shouldShowSettledCompressionReference(referenceText) && (sessionCompressionAnchor!==null || sessionCompressionAnchorKey || sessionCompressionSummary))
    ? (()=>{const row=document.createElement('div');row.innerHTML=`<div class="compression-turn"><div class="compression-turn-blocks">${_compressionReferenceCardHtml(referenceText,false)}${_preservedCompressionTaskListCardsHtml(preservedCompressionTaskMessages)}</div></div>`;return row.firstElementChild;})()
    : null;
  let preservedCompressionTaskCardsAttached=!!referenceNode;
  // Cache visWithIdx so expanding the render window (Load earlier) doesn't
  // re-scan S.messages from scratch.  Invalidate only when the message array
  // length changes — i.e. new messages arrived or session was truncated.
  if(!_visWithIdxCache || _visWithIdxCacheLen !== S.messages.length || _visWithIdxCacheSrc !== S.messages){
    const rebuilt=[];
    let ri=0;
    for(const m of S.messages){
      if(!m||!m.role||m.role==='tool'){ri++;continue;}
      if(_isContextCompactionMessage(m)){ri++;continue;}
      if(_isPreservedCompressionTaskListMessage(m)){ri++;continue;}
      if(_isRecoveryControlMessage(m)){ri++;continue;}
      const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
      const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
      const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
      if(msgContent(m)||m._statusCard||m.attachments?.length||(m.role==='assistant'&&(hasTc||hasTu||hasPartialTc||_messageHasReasoningPayload(m)||_assistantMessageHasVisibleContent(m)))) rebuilt.push({m,rawIdx:ri});
      ri++;
    }
    _visWithIdxCache=rebuilt;
    _visWithIdxCacheLen=S.messages.length;
    _visWithIdxCacheSrc=S.messages;
  }
  const visWithIdx=_visWithIdxCache;
  const preservedCompressionRawIdxs=[];
  let rawIdx=0;
  for(const m of S.messages){
    if(!m||!m.role||m.role==='tool'){rawIdx++;continue;}
    if(_isPreservedCompressionTaskListMessage(m)){preservedCompressionRawIdxs.push(rawIdx);rawIdx++;continue;}
    rawIdx++;
  }
  // Show a top affordance when earlier transcript content exists either in
  // memory (DOM windowing) or on the server (paginated session fetch).
  // Prefer expanding the local render window first so a fully loaded long
  // session can reduce DOM nodes without losing in-memory transcript data.
  const windowStart=Math.max(0, visWithIdx.length-renderWindowSize);
  const hiddenBeforeCount=windowStart;
  const renderVisWithIdx=visWithIdx.slice(windowStart);
  const firstRenderedRawIdx=renderVisWithIdx.length?renderVisWithIdx[0].rawIdx:Infinity;
  const hasServerOlder=!!(typeof _messagesTruncated!=='undefined' && _messagesTruncated && S.messages.length>0);
  const serverOlderCount=hasServerOlder&&Number.isFinite(Number(_oldestIdx))?Math.max(0,Number(_oldestIdx)):0;
  if(typeof _applySessionNavigationPrefs==='function') _applySessionNavigationPrefs();
  if(hiddenBeforeCount>0 || hasServerOlder){
    const indicator=document.createElement('button');
    indicator.type='button';
    indicator.id='loadOlderIndicator';
    indicator.className='load-older-indicator message-window-load-earlier';
    indicator.textContent=hiddenBeforeCount>0
      ? `Load earlier messages (${hiddenBeforeCount} hidden)`
      : (serverOlderCount>0
        ? `Load earlier messages (${serverOlderCount} older)`
        : (typeof t==='function'?t('load_older_messages'):'Load earlier messages'));
    indicator.onclick=()=>{
      if(hiddenBeforeCount>0) _showEarlierRenderedMessages();
      else if(typeof _loadOlderMessages==='function') _loadOlderMessages();
    };
    inner.appendChild(indicator);
    _wireMessageWindowLoadEarlierButton();
  }
  let lastUserRawIdx=-1;
  for(let i=visWithIdx.length-1;i>=0;i--){
    if(visWithIdx[i].m&&visWithIdx[i].m.role==='user'){
      lastUserRawIdx=visWithIdx[i].rawIdx;
      break;
    }
  }
  const insertionAnchorFull=_compressionAnchorIndex(
    visWithIdx,
    compressionState ? compressionState.anchorMessageKey : sessionCompressionAnchorKey,
    compressionState
      ? (typeof compressionState.anchorVisibleIdx==='number' ? compressionState.anchorVisibleIdx : compressionState.anchorRawIdx)
      : sessionCompressionAnchor
  );
  let insertionAnchor=null;
  if(typeof insertionAnchorFull==='number'){
    if(insertionAnchorFull<windowStart) insertionAnchor=renderVisWithIdx.length?0:null;
    else if(insertionAnchorFull<windowStart+renderVisWithIdx.length) insertionAnchor=insertionAnchorFull-windowStart;
    else insertionAnchor=renderVisWithIdx.length?renderVisWithIdx.length-1:null;
  }
  let _prevSepKey=null;
  let currentAssistantTurn=null;
  // Only build question→assistant mapping for the visible window, not the
  // full visWithIdx.  The jump-to-question button is only rendered for
  // assistant messages that appear in the current render window anyway.
  const questionRawIdxByAssistantRawIdx=new Map();
  // Seed lastQuestionRawIdx from hidden messages so the first visible
  // assistant message still gets a valid jump target even when its
  // corresponding user message sits just before the render window.
  let lastQuestionRawIdx=-1;
  for(let i=0;i<windowStart;i++){
    const role=visWithIdx[i]?.m?.role;
    if(role==='user') lastQuestionRawIdx=visWithIdx[i].rawIdx;
  }
  for(const entry of renderVisWithIdx){
    const role=entry&&entry.m&&entry.m.role;
    if(role==='user') lastQuestionRawIdx=entry.rawIdx;
    else if(role==='assistant') questionRawIdxByAssistantRawIdx.set(entry.rawIdx,lastQuestionRawIdx);
  }
  const assistantSegments=new Map();
  const assistantThinking=new Map();
  const userRows=new Map();
  // Only collect tool-call assistant indices for messages that are actually
  // rendered in the current window.  S.toolCalls can grow large in long turns,
  // but we only need the ones whose assistant_msg_idx falls inside the visible
  // range.
  const toolCallAssistantIdxs=new Set();
  if(Array.isArray(S.toolCalls)){
    const renderedRawIdxs=new Set(renderVisWithIdx.map(e=>e.rawIdx));
    for(const tc of S.toolCalls){
      if(!tc) continue;
      const idx=tc.assistant_msg_idx;
      if(idx!==undefined && renderedRawIdxs.has(idx)){
        toolCallAssistantIdxs.add(idx);
      }
    }
  }
  // Windowed render loop replaces the legacy full loop:
  // for(let vi=0;vi<visWithIdx.length;vi++)
  for(let vi=0;vi<renderVisWithIdx.length;vi++){
    const {m,rawIdx}=renderVisWithIdx[vi];
    const _tsSep=m._ts||m.timestamp;
    if(_tsSep){
      const _d=new Date(_tsSep*1000);
      const _key=_d.toDateString();
      if(_prevSepKey && _prevSepKey!==_key){
        const sep=document.createElement('div');
        sep.className='msg-date-sep';
        sep.textContent=_fmtDateSep(_d);
        inner.appendChild(sep);
      }
      _prevSepKey=_key;
    }
    let content=m.content||'';
    let thinkingText='';
    if(Array.isArray(content)){
      thinkingText=content.filter(p=>p&&(p.type==='thinking'||p.type==='reasoning')).map(p=>p.thinking||p.reasoning||p.text||'').join('\n');
      content=content.filter(p=>p&&p.type==='text').map(p=>p.text||p.content||'').join('\n');
    }
    if(!thinkingText && (m.reasoning_content || m.reasoning)) thinkingText=m.reasoning_content || m.reasoning;
    if(!thinkingText && typeof content==='string'){
      const thinkMatch=content.match(/^\s*<think>([\s\S]*?)<\/think>\s*/);
      if(thinkMatch){
        thinkingText=thinkMatch[1].trim();
        content=content.replace(/^\s*<think>[\s\S]*?<\/think>\s*/,'').trimStart();
      }
      if(!thinkingText){
        // Historical name "gemmaMatch" refers to MiniMax <|channel>thought format.
        const gemmaMatch=content.match(/^\s*<\|channel\|?>thought\n?([\s\S]*?)<channel\|>\s*/);
        if(gemmaMatch){
          thinkingText=gemmaMatch[1].trim();
          content=content.replace(/^\s*<\|channel\|?>thought\n?[\s\S]*?<channel\|>\s*/,'').trimStart();
        }
      }
      if(!thinkingText){
        // Gemma 4 uses asymmetric <|turn|>thinking\n...<turn|> delimiters.
        const gemmaTurnMatch=content.match(/^\s*<\|turn\|>thinking\n([\s\S]*?)<turn\|>\s*/);
        if(gemmaTurnMatch){
          thinkingText=gemmaTurnMatch[1].trim();
          content=content.replace(/^\s*<\|turn\|>thinking\n[\s\S]*?<turn\|>\s*/,'').trimStart();
        }
      }
    }
    const isUser=m.role==='user';
    if(!isUser&&_isMarkerOnlyAssistantCompressionMessage(m)){
      content='**Error:** No response received after context compression. Please retry.';
    }
    const displayContent=isUser?_stripAttachedFilesMarkerForDisplay(_stripWorkspaceDisplayPrefix(content)):content;
    if(thinkingText&&!isUser){
      thinkingText=_stripVisibleAssistantEchoFromThinking(thinkingText, displayContent);
    }
    const isLastAssistant=!isUser&&vi===renderVisWithIdx.length-1;
    const nextRendered=renderVisWithIdx[vi+1];
    const isTurnFinalAssistant=!isUser&&(!nextRendered||!nextRendered.m||nextRendered.m.role!=='assistant');
    let filesHtml='';
    if(m.attachments&&m.attachments.length){
      // Static regression tests intentionally look for msg-media-img/msg-file-badge near this branch.
      const _attachSid=(S.session&&S.session.session_id)||'';
      filesHtml=`<div class="msg-files">${m.attachments.map(f=>{
        const fLabel=typeof f==='string'?f:(f&&(f.name||f.filename||f.path))||'';
        const fname=String(fLabel).split('/').pop()||String(fLabel);
        // Use api/file/raw which resolves filename relative to the session workspace.
        const fileUrl='api/file/raw?session_id='+encodeURIComponent(_attachSid)+'&path='+encodeURIComponent(fname);
        return _renderAttachmentHtml(fname,fileUrl);
      }).join('')}</div>`;
    }
    let bodyHtml = _getCachedRender(displayContent, isUser);
    if(!isUser&&m.provider_details){
      const summary=m.provider_details_label||'Provider details';
      bodyHtml += `<details class="provider-error-details"><summary>${esc(String(summary))}</summary><pre><code>${esc(String(m.provider_details))}</code></pre></details>`;
    }
    const statusHtml = (!isUser&&m._statusCard) ? _statusCardHtml(m._statusCard) : '';
    const isEditableUser=isUser&&rawIdx===lastUserRawIdx;
    const editBtn  = isEditableUser ? `<button class="msg-action-btn" title="${t('edit_message')}" onclick="editMessage(this)">${li('pencil',13)}</button>` : '';
    const undoBtn  = isLastAssistant ? `<button class="msg-action-btn" title="${t('undo_exchange')}" onclick="undoLastExchange()">${li('undo',13)}</button>` : '';
    const retryBtn = isLastAssistant ? `<button class="msg-action-btn" title="${t('regenerate')}" onclick="regenerateResponse(this)">${li('rotate-ccw',13)}</button>` : '';
    const copyBtn  = `<button class="msg-copy-btn msg-action-btn" title="${t('copy')}" onclick="copyMsg(this)">${li('copy',13)}</button>`;
    const forkBtn  = `<button class="msg-action-btn" title="${t('fork_from_here')}" onclick="forkFromMessage(${rawIdx+1})">${li('git-branch',13)}</button>`;
    const ttsBtn   = !isUser ? `<button class="msg-action-btn msg-tts-btn" title="${t('tts_listen')||'Listen'}" onclick="speakMessage(this)">${li('volume-2',13)}</button>` : '';
    const tsVal=m._ts||m.timestamp;
    // _formatInServerTz handles fractional-hour offsets (India +0530 etc.)
    // correctly via offset arithmetic; bare toLocaleString is the browser-tz fallback.
    const _fmtSv=(typeof _formatInServerTz==='function')?_formatInServerTz:null;
    const tsTitle=tsVal?(_fmtSv?_fmtSv(new Date(tsVal*1000),{}):new Date(tsVal*1000).toLocaleString()):'';
    const tsTime=_formatMessageFooterTimestamp(tsVal);
    const timeHtml = tsTime ? `<span class="msg-time" title="${esc(tsTitle)}">${tsTime}</span>` : '';
    // #3114: show jump-to-question on every assistant message that has a
    // resolvable question target, not just the turn-final one. Multi-step
    // turns (tool_call -> assistant -> tool_call -> assistant) otherwise
    // strip the button from every intermediate assistant bubble and the
    // user loses the navigation affordance.
    const _qJumpTarget=(!isUser&&!m._live)?questionRawIdxByAssistantRawIdx.get(rawIdx):undefined;
    const questionJumpBtn = (_qJumpTarget!==undefined&&_qJumpTarget!==null)
      ? _questionJumpButtonHtml(_qJumpTarget)
      : '';
    const footHtml = `<div class="msg-foot">${timeHtml}<span class="msg-actions">${editBtn}${ttsBtn}${forkBtn}${copyBtn}${retryBtn}</span>${questionJumpBtn}</div>`;

    if(_isContextCompactionMessage(m)){
      continue;
    }

    if(isUser){
      currentAssistantTurn=null;
      const row=document.createElement('div');
      row.className='msg-row';
      row.id=_userMessageDomId(rawIdx);
      row.dataset.msgIdx=rawIdx;
      row.dataset.role='user';
      row.dataset.rawText=String(displayContent).trim();
      row.innerHTML=`${filesHtml}<div class="msg-body">${bodyHtml}</div>${footHtml}`;
      inner.appendChild(row);
      userRows.set(rawIdx, row);
      continue;
    }

    if(!currentAssistantTurn){
      currentAssistantTurn=_createAssistantTurn(tsTitle, isTpsDisplayEnabled()?_formatTurnTps(m._turnTps):'');
      inner.appendChild(currentAssistantTurn);
    }
    const seg=document.createElement('div');
    seg.className='assistant-segment';
    seg.dataset.msgIdx=rawIdx;
    seg.dataset.rawText=String(content).trim();
    if(m._live){
      currentAssistantTurn.id='liveAssistantTurn';
      // Stamp the session id on the live turn so finalizeThinkingCard()
      // and other late callbacks can verify they're operating on the
      // right session's DOM (the user may have switched tabs/sessions
      // while this stream is still streaming). See #1366.
      if(S.session) currentAssistantTurn.dataset.sessionId=S.session.session_id;
      seg.setAttribute('data-live-assistant','1');
    }
    if(_ERR_MSG_RE.test(String(content||'').trim())) seg.dataset.error='1';
    if(thinkingText&&window._showThinking!==false){
      if(isSimplifiedToolCalling()) assistantThinking.set(rawIdx, thinkingText);
      else if(window._showThinking!==false) seg.insertAdjacentHTML('beforeend', _thinkingCardHtml(thinkingText));
    }
    const hasVisibleBody=!!(String(content||'').trim()||filesHtml||statusHtml);
    if(statusHtml){
      seg.insertAdjacentHTML('beforeend', statusHtml);
    }else if(hasVisibleBody){
      seg.insertAdjacentHTML('beforeend', `${filesHtml}<div class="msg-body">${bodyHtml}</div>${footHtml}`);
    }else if(!(thinkingText&&window._showThinking!==false&&!isSimplifiedToolCalling())){
      seg.classList.add('assistant-segment-anchor');
    }
    _assistantTurnBlocks(currentAssistantTurn).appendChild(seg);
    assistantSegments.set(rawIdx, seg);
  }

  function _insertCompressionLikeNode(node, anchorIndex){
    if(!node) return;
    const anchorIdx=anchorIndex===undefined?insertionAnchor:anchorIndex;
    if(anchorIdx!==null && renderVisWithIdx[anchorIdx]){
      const anchorRawIdx=renderVisWithIdx[anchorIdx].rawIdx;
      const anchorSeg=assistantSegments.get(anchorRawIdx);
      if(anchorSeg){
        const turn=anchorSeg.closest('.assistant-turn');
        const blocks=_assistantTurnBlocks(turn);
        if(blocks){
          blocks.appendChild(node);
          return;
        }
      }
      const userRow=userRows.get(anchorRawIdx);
      if(userRow && userRow.parentElement){
        userRow.parentElement.insertBefore(node, userRow.nextSibling);
        return;
      }
    }
    inner.appendChild(node);
  }
  function _insertCompressionLikeNodeByRawIdx(node, rawIdx){
    if(!node) return;
    if(rawIdx<firstRenderedRawIdx) return;
    if(!renderVisWithIdx.length){
      inner.appendChild(node);
      return;
    }
    let anchorIdx=null;
    for(let i=0;i<renderVisWithIdx.length;i++){
      if(renderVisWithIdx[i].rawIdx > rawIdx){
        anchorIdx=i;
        break;
      }
    }
    if(anchorIdx===null){
      inner.appendChild(node);
      return;
    }
    const anchorRawIdx=renderVisWithIdx[anchorIdx].rawIdx;
    const anchorSeg=assistantSegments.get(anchorRawIdx);
    if(anchorSeg){
      const turn=anchorSeg.closest('.assistant-turn');
      const blocks=_assistantTurnBlocks(turn);
      if(blocks){
        blocks.insertBefore(node, anchorSeg);
        return;
      }
      const turnParent=turn && turn.parentElement;
      if(turnParent){
        turnParent.insertBefore(node, turn);
        return;
      }
    }
    const userRow=userRows.get(anchorRawIdx);
    if(userRow && userRow.parentElement){
      userRow.parentElement.insertBefore(node, userRow);
      return;
    }
    inner.appendChild(node);
  }
  const preservedOnlyNode=(!preservedCompressionTaskCardsAttached&&(!referenceNode||compressionState)&&preservedCompressionTaskMessages.length)
    ? (()=>{const row=document.createElement('div');row.innerHTML=`<div class="compression-turn"><div class="compression-turn-blocks">${_preservedCompressionTaskListCardsHtml(preservedCompressionTaskMessages)}</div></div>`;return row.firstElementChild;})()
    : null;
  const preservedOnlyAnchor=preservedCompressionRawIdxs.length
    ? (()=>{let idx=null;for(let i=0;i<renderVisWithIdx.length;i++){if(renderVisWithIdx[i].rawIdx<preservedCompressionRawIdxs[0]) idx=i;}return idx;})()
    : null;
  const handoffSummaryStates=_collectHandoffSummaryStates(S.messages);

  _insertCompressionLikeNode(compressionNode);
  if(referenceNode&&referenceMessageRawIdx>=0) _insertCompressionLikeNodeByRawIdx(referenceNode, referenceMessageRawIdx);
  else _insertCompressionLikeNode(referenceNode);
  _insertCompressionLikeNode(preservedOnlyNode, preservedOnlyAnchor);
  _insertCompressionLikeNode(handoffState?_handoffCardsNode(handoffState):null, renderVisWithIdx.length?renderVisWithIdx.length-1:null);
  for(const entry of handoffSummaryStates){
    if(!entry||!entry.state) continue;
    if(entry.rawIdx<firstRenderedRawIdx) continue;
    _insertCompressionLikeNodeByRawIdx(_handoffCardsNode(entry.state), entry.rawIdx);
  }
  renderCompressionUi();
  // Insert settled tool call cards (history view only).
  // During live streaming, tool cards are rendered in #liveToolCards by the
  // tool SSE handler and never mixed into the message list until done fires.
  //
  // Fallback: if S.toolCalls is empty (sessions that predate session-level tool
  // tracking, or runs that didn't go through the normal streaming path), build
  // a display list from per-message tool_calls (OpenAI format) stored in each
  // assistant message. This covers the reload case described in issue #140.
  if(!S.busy && (!S.toolCalls||!S.toolCalls.length)){
    // Index tool outputs by tool_call_id / tool_use_id so the
    // fallback-built cards carry their result snippet (not just the command).
    // Without this step CLI-origin sessions reload with empty tool cards.
    const resultsByTid={};
    const fallbackToolSources=[];
    S.messages.forEach((m,rawIdx)=>{
      if(!m) return;
      // OpenAI / Hermes CLI format: role=tool with tool_call_id
      if(m.role==='tool'){
        const tid=m.tool_call_id||m.tool_use_id||'';
        if(tid) resultsByTid[tid]=_cliToolResultSnippet(m.content);
        return;
      }
      // Anthropic format: tool_result blocks inside a user message content array
      if(Array.isArray(m.content)){
        m.content.forEach(p=>{
          if(!p||typeof p!=='object'||p.type!=='tool_result') return;
          const tid=p.tool_use_id||'';
          if(!tid) return;
          const raw=typeof p.content==='string'?p.content
                   :Array.isArray(p.content)?p.content.map(c=>c&&c.text?c.text:'').join('')
                   :'';
          resultsByTid[tid]=_cliToolResultSnippet(raw);
        });
      }
      if(m.role==='assistant'){
        const hasTopLevelToolCalls=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
        const hasContentToolUse=Array.isArray(m.content)&&m.content.some(p=>p&&typeof p==='object'&&p.type==='tool_use');
        const hasPartialToolCalls=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
        if(hasTopLevelToolCalls||hasContentToolUse||hasPartialToolCalls) fallbackToolSources.push({m,rawIdx});
      }
    });
    const derived=[];
    fallbackToolSources.forEach(({m,rawIdx})=>{
      // OpenAI format: top-level tool_calls field on the assistant message
      (m.tool_calls||[]).forEach(tc=>{
        if(!tc||typeof tc!=='object') return;
        const fn=tc.function||{};
        const name=fn.name||tc.name||'tool';
        let args={};
        try{ args=JSON.parse(fn.arguments||'{}'); }catch(e){}
        const tid=tc.id||tc.call_id||'';
        const patchSnippet=_cliPatchSnippetFromArgs(name,args);
        const resultSnippet=resultsByTid[tid]||'';
        let argsSnap={};
        Object.keys(args).slice(0,4).forEach(k=>{ const v=String(args[k]); argsSnap[k]=v.slice(0,120)+(v.length>120?'...':''); });
        derived.push({
          name,
          snippet:_cliToolCardSnippet(resultSnippet,patchSnippet),
          is_diff:_cliToolCardHasDiffSnippet(resultSnippet,patchSnippet),
          tid,
          assistant_msg_idx:rawIdx,
          args:argsSnap,
          done:true,
        });
      });
      // Anthropic format: tool_use blocks inside assistant content array
      if(Array.isArray(m.content)){
        m.content.forEach(p=>{
          if(!p||typeof p!=='object'||p.type!=='tool_use') return;
          const name=p.name||'tool';
          const args=p.input||{};
          const tid=p.id||'';
          const patchSnippet=_cliPatchSnippetFromArgs(name,args);
          const resultSnippet=resultsByTid[tid]||'';
          const argsSnap={};
          if(args && typeof args==='object'){
            Object.keys(args).slice(0,4).forEach(k=>{ const v=String(args[k]); argsSnap[k]=v.slice(0,120)+(v.length>120?'...':''); });
          }
          derived.push({
            name,
            snippet:_cliToolCardSnippet(resultSnippet,patchSnippet),
            is_diff:_cliToolCardHasDiffSnippet(resultSnippet,patchSnippet),
            tid,
            assistant_msg_idx:rawIdx,
            args:argsSnap,
            done:true,
          });
        });
      }
      // WebUI-internal partial tool calls captured on cancel/stop
      // (private shape: name/args/done/preview/snippet, no OpenAI envelope).
      if(Array.isArray(m._partial_tool_calls)){
        m._partial_tool_calls.forEach(tc=>{
          if(!tc||typeof tc!=='object') return;
          const name=tc.name||'tool';
          const args=tc.args||{};
          const tid=tc.id||tc.call_id||tc.tool_call_id||tc.tid||'';
          const patchSnippet=_cliPatchSnippetFromArgs(name,args);
          const resultSnippet=_cliToolResultSnippet(tc.snippet||tc.result||tc.output||tc.preview||'');
          const argsSnap={};
          if(args && typeof args==='object'){
            Object.keys(args).slice(0,4).forEach(k=>{ const v=String(args[k]); argsSnap[k]=v.slice(0,120)+(v.length>120?'...':''); });
          }
          derived.push({
            name,
            snippet:_cliToolCardSnippet(resultSnippet,patchSnippet),
            is_diff:_cliToolCardHasDiffSnippet(resultSnippet,patchSnippet),
            tid,
            assistant_msg_idx:rawIdx,
            args:argsSnap,
            done:true,
          });
        });
      }
    });
    if(derived.length) S.toolCalls=derived;
  }
  // Render tool cards: allow during streaming when S.toolCalls is already
  // populated (e.g. from INFLIGHT restore or SSE events). Only the fallback
  // derivation above is blocked by S.busy — DOM insertion should proceed
  // whenever tool cards exist.
  if(!S.busy || (S.toolCalls&&S.toolCalls.length)){
    inner.querySelectorAll('.tool-call-group:not([data-compression-card]),.tool-card-row:not([data-compression-card]),.agent-activity-thinking:not([data-live-thinking="1"])').forEach(el=>el.remove());
    const byAssistant = {};
    for(const tc of (S.toolCalls||[])){
      const key = tc.assistant_msg_idx !== undefined ? tc.assistant_msg_idx : -1;
      if(!byAssistant[key]) byAssistant[key] = [];
      byAssistant[key].push(tc);
    }
    const assistantIdxs=[...assistantSegments.keys()].sort((a,b)=>a-b);
    const anchorInsertAfter = new Map();
    if(isSimplifiedToolCalling()){
      const activityIdxs=[...new Set([...Object.keys(byAssistant).map(k=>parseInt(k)), ...assistantThinking.keys()])].sort((a,b)=>a-b);
      for(const aIdx of activityIdxs){
        const cards=byAssistant[aIdx]||[];
        if(!cards.length&&assistantThinking.has(aIdx)){
          const anchorRow=assistantSegments.get(aIdx);
          if(anchorRow&&window._showThinking!==false){
            anchorRow.insertAdjacentHTML('beforeend',_thinkingCardHtml(assistantThinking.get(aIdx)));
          }
          continue;
        }
        let anchorRow=assistantSegments.get(aIdx)||null;
        if(!anchorRow&&assistantIdxs.length){
          if(aIdx<assistantIdxs[0]) continue;
          const fallbackIdx=[...assistantIdxs].reverse().find(idx=>idx<=aIdx);
          anchorRow=fallbackIdx!==undefined?assistantSegments.get(fallbackIdx):assistantSegments.get(assistantIdxs[assistantIdxs.length-1]);
        }
        if(!anchorRow) continue;
        const anchorParent=anchorRow.parentElement;
        let insertAfterNode = anchorInsertAfter.get(anchorRow) || anchorRow;
        const group=ensureActivityGroup(anchorParent,{collapsed:true,anchor:insertAfterNode,activityKey:`assistant:${aIdx}`});
        const sourceMsg=S.messages[aIdx]||{};
        if(sourceMsg._turnDuration!==undefined) group.setAttribute('data-turn-duration', String(sourceMsg._turnDuration));
        const body=group&&group.querySelector('.tool-call-group-body');
        if(!body) continue;
        const thinkingText=assistantThinking.get(aIdx);
        if(thinkingText){
          body.appendChild(_thinkingActivityNode(thinkingText, false));
        }
        for(const tc of cards){
          body.appendChild(buildToolCard(tc));
        }
        _syncToolCallGroupSummary(group);
        if(anchorRow) anchorInsertAfter.set(anchorRow, group);
      }
    }else if(S.toolCalls && S.toolCalls.length){
      for(const [key, cards] of Object.entries(byAssistant)){
        const aIdx = parseInt(key);
        let anchorRow=assistantSegments.get(aIdx)||null;
        if(!anchorRow&&assistantIdxs.length){
          if(aIdx<assistantIdxs[0]) continue;
          const fallbackIdx=[...assistantIdxs].reverse().find(idx=>idx<=aIdx);
          anchorRow=fallbackIdx!==undefined?assistantSegments.get(fallbackIdx):assistantSegments.get(assistantIdxs[assistantIdxs.length-1]);
        }
        if(!anchorRow) continue;
        const anchorParent=anchorRow.parentElement;
        const frag=document.createDocumentFragment();
        let lastInsertedNode=null;
        for(const tc of cards){
          const card=buildToolCard(tc);
          frag.appendChild(card);
          lastInsertedNode=card;
        }
        // Add expand/collapse toggle for groups with 2+ cards
        if(cards.length>=2){
          const toggle=document.createElement('div');
          toggle.className='tool-cards-toggle';
          // Collect card elements before they get moved to DOM
          const cardEls=Array.from(frag.querySelectorAll('.tool-card'));
          const expandBtn=document.createElement('button');
          expandBtn.textContent=t('expand_all');
          expandBtn.onclick=()=>cardEls.forEach(c=>c.classList.add('open'));
          const collapseBtn=document.createElement('button');
          collapseBtn.textContent=t('collapse_all');
          collapseBtn.onclick=()=>cardEls.forEach(c=>c.classList.remove('open'));
          toggle.appendChild(expandBtn);
          toggle.appendChild(collapseBtn);
          frag.insertBefore(toggle,frag.firstChild);
        }
        const insertAfterNode = anchorInsertAfter.get(anchorRow) || anchorRow;
        const refNode = insertAfterNode ? insertAfterNode.nextSibling : null;
        if(refNode) anchorParent.insertBefore(frag,refNode);
        else anchorParent.appendChild(frag);
        if(anchorRow&&lastInsertedNode) anchorInsertAfter.set(anchorRow, lastInsertedNode);
      }
    }
  }
  // Render per-turn duration and optional token usage on assistant messages.
  // Duration stays visible even when token usage is disabled, because it answers
  // the basic "how long did that turn take?" UX question. Only walk rendered
  // assistant segments so hidden messages above the DOM window cannot skew the
  // footer-to-message mapping.
  {
    const renderedAssistantIdxs=[...assistantSegments.keys()].sort((a,b)=>a-b);
    for(const mi of renderedAssistantIdxs){
      const msg=S.messages[mi]||{};
      if(msg.role!=='assistant') continue;
      const routing=msg._gatewayRouting||null;
      const gatewayText=_formatGatewayModelLabel(S.session&&S.session.model||'', '', routing);
      const failoverText=_gatewayRoutingFailoverText(routing);
      const modelWarningText=_gatewayModelWarningText(routing);
      const hasTurnUsage=!!msg._turnUsage;
      // The activity-group summary owns the "Done in …" duration ONLY when a
      // group is actually created. A tool-call turn always builds one. A
      // thinking-only turn under Simplified Tool Calling now renders thinking
      // inline (no group — see the `continue` at the activityIdxs loop, #3592),
      // so it must keep its footer duration; suppressing it there would silently
      // drop "Done in …" for thinking-only turns (#3592 review).
      const compactActivityForMessage=isSimplifiedToolCalling()&&toolCallAssistantIdxs.has(mi);
      const durationText=compactActivityForMessage?'':_formatTurnDuration(msg._turnDuration);
      if(!hasTurnUsage&&!durationText&&!gatewayText&&!failoverText&&!modelWarningText) continue;
      const seg=assistantSegments.get(mi);
      const row=seg?seg.closest('.assistant-turn'):null;
      const footerRows=row?row.querySelectorAll('.msg-foot'):[];
      const targetFoot=footerRows.length?footerRows[footerRows.length-1]:null;
      if(!targetFoot||targetFoot.querySelector('.msg-usage-inline,.msg-duration-inline,.msg-gateway-inline,.gateway-failover-inline,.msg-model-warning-inline')) continue;
      const fragments=[];
      if(modelWarningText){
        const warning=document.createElement('span');
        warning.className='msg-model-warning-inline';
        warning.textContent=modelWarningText;
        fragments.push(warning);
      }
      if(failoverText){
        const failover=document.createElement('span');
        failover.className='gateway-failover-inline';
        failover.textContent=failoverText;
        fragments.push(failover);
      }
      if(gatewayText){
        const gateway=document.createElement('span');
        gateway.className='msg-gateway-inline';
        gateway.textContent=gatewayText;
        fragments.push(gateway);
      }
      if(durationText){
        const duration=document.createElement('span');
        duration.className='msg-duration-inline';
        duration.textContent=`Done in ${durationText}`;
        fragments.push(duration);
      }
      if(window._showTokenUsage&&hasTurnUsage){
        const usage=document.createElement('span');
        usage.className='msg-usage-inline';
        const inTok=msg._turnUsage.input_tokens||0;
        const outTok=msg._turnUsage.output_tokens||0;
        const cost=msg._turnUsage.estimated_cost;
        let text=`${_fmtTokens(inTok)} in · ${_fmtTokens(outTok)} out`;
        if(cost) text+=` · ~$${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
        const cacheHitPct=msg._turnUsage.cache_hit_percent;
        if(cacheHitPct!=null) text+=` · ${t('usage_cached_percent',cacheHitPct)}`;
        usage.textContent=text;
        fragments.push(usage);
      }
      if(fragments.length){
        targetFoot.classList.add('msg-foot-with-usage');
        for(let i=fragments.length-1;i>=0;i--) targetFoot.insertBefore(fragments[i], targetFoot.firstChild);
      }
    }
  }
  // Only force-scroll when not actively streaming — mid-stream re-renders
  // (tool completion, session switch) must not override the user's scroll position.
  // scrollIfPinned() respects _scrollPinned, so it's a no-op if user scrolled up.
  _scrollAfterMessageRender(preserveScroll, scrollSnapshot);
  // Apply syntax highlighting after DOM is built
  requestAnimationFrame(()=>postProcessRenderedMessages(inner));
  // Refresh todo panel if it's currently open
  if(typeof loadTodos==='function' && document.getElementById('panelTodos') && document.getElementById('panelTodos').classList.contains('active')){
    loadTodos();
  }
  // Apply persisted playback speed after media nodes are rendered.
  if(typeof _applyMediaPlaybackPreferences==='function') _applyMediaPlaybackPreferences(inner);
  // Populate session cache so switching back here skips a full rebuild.
  _sessionHtmlCacheSid=sid;
  if(sid&&!INFLIGHT[sid]&&!hasTransientTranscriptUi){
    const _html=inner.innerHTML;
    // Only cache sessions with <300KB rendered HTML; evict oldest beyond 8 sessions.
    if(_html.length<300_000){
      const renderSignature=cachedRenderSignature===null?_messageRenderCacheSignature():cachedRenderSignature;
      _sessionHtmlCache.set(sid,{html:_html,msgCount,renderWindowSize,signature:renderSignature});
      if(_sessionHtmlCache.size>8){_sessionHtmlCache.delete(_sessionHtmlCache.keys().next().value);}
    }
  }
}

function _toolDisplayName(tc){
  const name=(tc&&tc.name)||'tool';
  if(name==='subagent_progress') return 'Subagent';
  if(name==='delegate_task') return 'Delegate task';
  return name;
}

// Activity-summary detection for persisted memory/skill writes (#3340, #3544).
// Action vocabularies match the real agent tool enums:
//   memory.action      = add | replace | remove   (add/replace persist content → "saved")
//   skill_manage.action= create | patch | edit | delete | write_file | remove_file
//                        (create/patch/edit/write_file mutate a skill → "updated")
// Deletions (memory 'remove', skill 'delete'/'remove_file') are intentionally
// excluded so the "saved"/"updated" label verbs stay accurate; running/errored
// calls are excluded so only completed writes are counted.
const _MEMORY_SAVE_ACTIONS=new Set(['add','replace']);
const _SKILL_UPDATE_ACTIONS=new Set(['create','patch','edit','write_file']);
function _tcAction(tc){
  return String((tc&&tc.args&&tc.args.action)||'').toLowerCase();
}
function _isMemorySave(tc){
  if(!tc||tc.name!=='memory'||tc.done===false||tc.is_error) return false;
  return _MEMORY_SAVE_ACTIONS.has(_tcAction(tc));
}
function _isSkillUpdate(tc){
  if(!tc||tc.name!=='skill_manage'||tc.done===false||tc.is_error) return false;
  return _SKILL_UPDATE_ACTIONS.has(_tcAction(tc));
}
function toolIcon(name){
  const icons={
    terminal:        li('terminal'),
    read_file:       li('file-text'),
    write_file:      li('file-pen'),
    search_files:    li('search'),
    web_search:      li('globe'),
    web_extract:     li('globe'),
    execute_code:    li('play'),
    patch:           li('wrench'),
    memory:          li('brain'),
    skill_manage:    li('book-open'),
    todo:            li('list-todo'),
    cronjob:         li('clock'),
    delegate_task:   li('bot'),
    send_message:    li('message-square'),
    browser_navigate:li('globe'),
    vision_analyze:  li('eye'),
    subagent_progress:li('shuffle'),
  };
  return icons[name]||li('wrench');
}

function _toolArgPreviewValue(value){
  if(value===null||value===undefined) return '';
  if(Array.isArray(value)){
    if(!value.length) return '[]';
    if(value.length<=3&&value.every(v=>v===null||['string','number','boolean'].includes(typeof v))){
      return value.map(v=>String(v)).join(', ');
    }
    return `${value.length} items`;
  }
  if(typeof value==='object') return 'object';
  return String(value).replace(/\s+/g,' ').trim();
}
// Secret/sensitive-arg guard for collapsed tool-card previews. Exact-name hiding
// alone misses camelCase / variant spellings (apiKey, access_token, clientSecret,
// Authorization, …), so a normalized substring check runs first so secret-shaped
// argument names are never surfaced in the always-visible collapsed header (#3267).
function _toolArgPreviewKeyIsHidden(key){
  const k=String(key||'').toLowerCase().replace(/[^a-z0-9]/g,'');
  // verbose-but-not-secret bodies we keep out of the compact preview
  const verbose=['content','filecontent','newstring','oldstring','patch','text','message','prompt','code','script','cookies','headers'];
  if(verbose.includes(k)) return true;
  // secret-shaped substrings (covers api_key/apiKey, access_token/auth_token/bearer,
  // client_secret, password, credential, private_key, authorization, etc.)
  return /(apikey|token|secret|password|passwd|credential|authorization|\bauth\b|auth$|^auth|bearer|privatekey|accesskey|sessionkey|signingkey|cookie)/.test(k)
    || k==='auth' || k==='key' || k==='pat';
}
function _formatToolArgPreview(args){
  if(!args||typeof args!=='object') return '';
  const preferred=['path','file_path','target','pattern','query','url','urls','name','ref','command','action','mode','schedule','workdir'];
  const keys=[];
  for(const key of preferred){
    if(Object.prototype.hasOwnProperty.call(args,key)&&!_toolArgPreviewKeyIsHidden(key)) keys.push(key);
  }
  for(const key of Object.keys(args)){
    if(keys.length>=3) break;
    if(keys.includes(key)||_toolArgPreviewKeyIsHidden(key)) continue;
    keys.push(key);
  }
  const parts=[];
  for(const key of keys){
    const raw=_toolArgPreviewValue(args[key]);
    if(!raw) continue;
    const val=raw.length>96?`${raw.slice(0,93)}…`:raw;
    parts.push(`${key}=${val}`);
    if(parts.join(' · ').length>=150) break;
  }
  const out=parts.join(' · ');
  return out.length>180?`${out.slice(0,177)}…`:out;
}
function _toolCardPreviewText(tc, displaySnippet){
  const explicit=String(tc&&tc.preview||'').trim();
  if(explicit) return explicit;
  const argPreview=_formatToolArgPreview(tc&&tc.args);
  if(argPreview) return argPreview;
  if(tc&&tc.done===false) return 'Running';
  if(tc&&tc.is_error) return 'Failed';
  return 'Completed';
}
function buildToolCard(tc){
  const row=document.createElement('div');
  row.className='tool-card-row';
  const icon=toolIcon(tc.name);
  const hasDetail=(tc.snippet&&tc.snippet!==tc.preview)||(tc.args&&Object.keys(tc.args).length>0);
  let displaySnippet='';
  if(tc.snippet){
    const s=tc.snippet;
    if(s.length<=800){displaySnippet=s;}
    else{
      const cutoff=s.slice(0,800);
      const lastBreak=Math.max(cutoff.lastIndexOf('. '),cutoff.lastIndexOf('\n'),cutoff.lastIndexOf('; '));
      displaySnippet=lastBreak>80?s.slice(0,lastBreak+1):cutoff;
    }
  }
  const hasMore=tc.snippet&&tc.snippet.length>displaySnippet.length;
  const moreLabel=tc.is_diff?'Show diff':'Show more';
  const lessLabel=tc.is_diff?'Hide diff':'Show less';
  const runIndicator=tc.done===false?'<span class="tool-card-running-dot"></span>':'';
  const isSubagent=tc.name==='subagent_progress';
  const isDelegation=tc.name==='delegate_task';
  const cardClass='tool-card'+(tc.done===false?' tool-card-running':'')+(isSubagent?' tool-card-subagent':'');
  // Clean up legacy subagent prefixes since the Lucide icon already shows it
  let displayName=_toolDisplayName(tc);
  let previewText=_toolCardPreviewText(tc, displaySnippet);
  if(isSubagent) previewText=previewText.replace(/^(?:\u{1F500}|↳)\s*/u,'');
  row.innerHTML=`
    <div class="${cardClass}">
      <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
        ${runIndicator}
        <span class="tool-card-icon">${icon}</span>
        <span class="tool-card-name">${esc(displayName)}</span>
        <span class="tool-card-preview">${esc(previewText)}</span>
        ${hasDetail?`<span class="tool-card-toggle">${li('chevron-right',12)}</span>`:''}
      </div>
      ${hasDetail?`<div class="tool-card-detail">
        ${tc.args&&Object.keys(tc.args).length?`<div class="tool-card-args">${
          Object.entries(tc.args).map(([k,v])=>`<div><span class="tool-arg-key">${esc(k)}</span> <span class="tool-arg-val">${esc(String(v))}</span></div>`).join('')
        }</div>`:''}
        ${displaySnippet?`<div class="tool-card-result">
          <pre>${tc.is_diff||_snippetLooksLikeDiff(displaySnippet)?`<code class="diff-block" data-highlighted="1">${_colorDiffLines(displaySnippet)}</code>`:esc(displaySnippet)}</pre>
          ${hasMore?`<button class="tool-card-more" data-full="${esc(tc.snippet||'').replace(/"/g,'&quot;')}" data-short="${esc(displaySnippet||'').replace(/"/g,'&quot;')}" data-is-diff="${tc.is_diff||_snippetLooksLikeDiff(displaySnippet)?1:0}" data-more-label="${esc(moreLabel)}" data-less-label="${esc(lessLabel)}" onclick="event.stopPropagation();_toggleToolDiff(this)">${esc(moreLabel)}</button>`:''}
        </div>`:''}
      </div>`:''}
    </div>`;
  row._tcData = tc;
  // Durable classification flags: _tcData (a JS property) does NOT survive the
  // outerHTML/innerHTML snapshot+restore the live tool-call group uses on session
  // switch/restore, which would make _syncToolCallGroupSummary re-count restored
  // memory/skill rows as generic tools and silently drop the suffix. Mirror the
  // classification onto data-* attributes so it survives serialization. (#3544)
  if(_isMemorySave(tc)){row.setAttribute('data-memory-save','1');row.removeAttribute('data-skill-update');}
  else if(_isSkillUpdate(tc)){row.setAttribute('data-skill-update','1');row.removeAttribute('data-memory-save');}
  else {row.removeAttribute('data-memory-save');row.removeAttribute('data-skill-update');}
  return row;
}

function _colorDiffLines(text){
  if(typeof text !== 'string') return esc(String(text||''));
  return esc(text).split('\n').map(line=>{
    if(line.startsWith('@@')) return `<span class="diff-line diff-hunk">${line}</span>`;
    if(line.startsWith('+')&&!line.startsWith('+++')) return `<span class="diff-line diff-plus">${line}</span>`;
    if(line.startsWith('-')&&!line.startsWith('---')) return `<span class="diff-line diff-minus">${line}</span>`;
    return `<span class="diff-line">${line}</span>`;
  }).join('\n');
}

// Detect if text looks like a unified diff (has @@ hunk headers and +/- lines).
function _snippetLooksLikeDiff(text){
  if(typeof text!=='string'||text.length<10) return false;
  if(!/^@@\s/.test(text)) return false;
  const lines=text.split('\n');
  let plusMinus=0;
  for(let i=0;i<lines.length&&i<50;i++){
    const l=lines[i];
    if(l.startsWith('+')||l.startsWith('-')) plusMinus++;
  }
  return plusMinus>=2;
}

function _toggleToolDiff(btn){
  const pre=btn.closest('.tool-card-result')?.querySelector('pre');
  if(!pre) return;
  const isDiff=btn.dataset.isDiff==='1';
  const expanded=btn.textContent===btn.dataset.moreLabel;
  const raw=expanded?btn.dataset.full:btn.dataset.short;
  if(isDiff){
    let code=pre.querySelector('code');
    if(!code){code=document.createElement('code');code.className='diff-block';pre.textContent='';pre.appendChild(code);}
    code.innerHTML=_colorDiffLines(raw);
  }else{
    pre.textContent=raw;
  }
  btn.textContent=expanded?btn.dataset.lessLabel:btn.dataset.moreLabel;
}

function _syncToolCallGroupSummary(group){
  if(!group) return;
  const cards=Array.from(group.querySelectorAll('.tool-card-row .tool-card'));
  const toolCount=cards.length;
  const label=group.querySelector('.tool-call-group-label');
  const durationEl=group.querySelector('.tool-call-group-duration');
  if(label){
    const rows=Array.from(group.querySelectorAll('.tool-card-row'));
    // Prefer the live _tcData classification; fall back to the durable data-*
    // flags for rows restored from an HTML snapshot (which drops JS properties).
    const isMem=r=>_isMemorySave(r._tcData)||r.getAttribute('data-memory-save')==='1';
    const isSkill=r=>_isSkillUpdate(r._tcData)||r.getAttribute('data-skill-update')==='1';
    const memCount=rows.filter(isMem).length;
    const skillCount=rows.filter(r=>!isMem(r)&&isSkill(r)).length;
    const otherCount=Math.max(0, toolCount-memCount-skillCount);
    let suffix='';
    if(memCount) suffix+=`, ${memCount} ${memCount===1?'memory':'memories'} saved`;
    if(skillCount) suffix+=`, ${skillCount} ${skillCount===1?'skill':'skills'} updated`;
    const toolsPart=otherCount?`${otherCount} tool${otherCount===1?'':'s'}`:'';
    if(group.getAttribute('data-live-tool-call-group')==='1'){
      if(toolsPart) label.textContent=`Activity: ${toolsPart}${suffix}`;
      else if(suffix) label.textContent=`Activity: ${suffix.slice(2)}`;
      else label.textContent='Activity · Running';
    }else if(toolsPart||suffix){
      label.textContent=toolsPart?`Activity: ${toolsPart}${suffix}`:`Activity: ${suffix.slice(2)}`;
    }else label.textContent='Activity';
    label.setAttribute('data-sweep-label', label.textContent);
  }
  if(durationEl){
    if(group.getAttribute('data-live-tool-call-group')==='1'){
      const activeText=_activityElapsedLabel(group);
      const progressText=_activityLiveProgressLabel(group);
      if(activeText) group.setAttribute('data-active-turn-elapsed',activeText);
      else group.removeAttribute('data-active-turn-elapsed');
      durationEl.textContent=[progressText, activeText].filter(Boolean).join(' · ');
      durationEl.style.display=durationEl.textContent?'':'none';
    }else{
      const durationText=_formatTurnDuration(group.dataset.turnDuration);
      durationEl.textContent=durationText?`Done in ${durationText}`:'';
      durationEl.style.display=durationText?'':'none';
    }
  }
}

function _activityProgressLabelForToolName(name){
  const key=String(name||'').toLowerCase().replace(/[^a-z0-9]+/g,'_');
  if(!key) return 'Working';
  if(key.includes('search')||key.includes('grep')) return 'Searching workspace';
  if(key.includes('read')||key.includes('view')||key.includes('open')) return 'Reading files';
  if(key.includes('write')||key.includes('patch')||key.includes('edit')) return 'Updating files';
  if(key.includes('terminal')||key.includes('shell')||key.includes('command')||key.includes('process')) return 'Running command';
  if(key.includes('web')||key.includes('fetch')||key.includes('curl')) return 'Checking web data';
  if(key.includes('todo')||key.includes('plan')) return 'Planning next steps';
  return 'Working';
}

function _activityLatestToolName(group){
  if(!group) return '';
  const running=group.querySelector('.tool-card.tool-card-running .tool-card-name');
  const latest=running || Array.from(group.querySelectorAll('.tool-card-name')).pop();
  return latest?String(latest.textContent||'').trim():'';
}

function _activityWaitingDetail(group,label=''){
  const toolName=_activityLatestToolName(group);
  if(toolName){
    const action=_activityProgressLabelForToolName(toolName);
    if(group&&group.querySelector('.tool-card.tool-card-running')) return `${action}: ${toolName}. Results will appear here.`;
    return `Last step: ${action} (${toolName}); now choosing the next action or composing a response.`;
  }
  if(String(label||'').toLowerCase().includes('model')) return 'Reviewing the prompt and context, then choosing the next action or composing the response.';
  return 'The agent is running; tool results and response text will appear here.';
}

function _activityLiveProgressLabel(group){
  if(!group||group.getAttribute('data-live-tool-call-group')!=='1') return '';
  const idleAge=_activityLastObservedAge(group);
  if(idleAge!==null&&idleAge>=90) return `No recent activity for ${_formatActiveElapsedTimer(idleAge)}`;
  const running=group.querySelector('.tool-card.tool-card-running .tool-card-name');
  const latest=running?String(running.textContent||'').trim():_activityLatestToolName(group);
  const waiting=group.querySelector('.agent-activity-status-waiting .agent-activity-status-label');
  if(latest) return _activityProgressLabelForToolName(latest);
  if(waiting&&waiting.textContent&&String(waiting.textContent).toLowerCase().includes('model')) return 'Reviewing prompt and context';
  if(waiting&&waiting.textContent) return waiting.textContent;
  return 'Starting agent';
}

// ── Live tool card helpers (called during SSE streaming) ──
// Live cards are inserted INLINE inside #msgInner (tagged with data-live-tid)
// so the streaming layout matches the settled layout produced by renderMessages
// (user → thinking → tool cards → response). The legacy #liveToolCards
// sibling container is no longer used for placement — keeping the cards in the
// message column eliminates the visible "jump" users saw when renderMessages
// fired on the done event.
function appendLiveToolCard(tc){
  // Guard: ignore if session was switched. Prevents stale tool events from
  // a previous session's SSE stream from manipulating the new session's DOM.
  if(!S.session||!S.activeStreamId) return;
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    if(S.session) turn.dataset.sessionId=S.session.session_id;  // see #1366
    $('msgInner').appendChild(turn);
  }
  const inner=_assistantTurnBlocks(turn);
  if(!inner) return;
  const tid=tc.tid||'';
  if(!isSimplifiedToolCalling()){
    // Update existing card in place (tool_complete after tool_start)
    if(tid){
      const existing=inner.querySelector(`.tool-card-row[data-live-tid="${CSS.escape(tid)}"]`);
      if(existing){
        const replacement=buildToolCard(tc);
        replacement.dataset.liveTid=tid;
        existing.replaceWith(replacement);
        // Keep #toolRunningRow alive — dots stay until text starts streaming
        // or the next tool fires (which replaces them). Removing here caused
        // a gap between tool completion and the first text token arriving.
        return;
      }
    }
    const row=buildToolCard(tc);
    if(tid) row.dataset.liveTid=tid;
    // Insert after whichever comes last: the current live assistant segment or
    // the last tool card. This handles both cases:
    //   text → tool1 → tool2  (no text between tools: anchor is card1)
    //   text1 → tool1 → text2 → tool2  (text between tools: anchor is text2)
    const children=Array.from(inner.children);
    // Include .thinking-card-row so tool cards land AFTER a finalized thinking
    // card, not between the text segment and thinking.
    const anchor=children.filter(el=>el.matches('[data-live-assistant="1"],.tool-card-row,.thinking-card-row')).pop();
    if(anchor) anchor.insertAdjacentElement('afterend', row);
    else inner.appendChild(row);
    // Add a 3-dot waiting indicator below the tool card so there's visual
    // feedback while the tool is running. Removed when text starts streaming
    // (ensureAssistantRow) or when tool_complete fires.
    const oldWait=$('toolRunningRow');if(oldWait)oldWait.remove();
    const waitRow=document.createElement('div');
    waitRow.id='toolRunningRow';
    waitRow.className='assistant-segment';
    waitRow.innerHTML='<div class="thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>';
    row.insertAdjacentElement('afterend', waitRow);
    if(typeof scrollIfPinned==='function') scrollIfPinned();
    return;
  }
  const children=Array.from(inner.children);
  const anchor=children.filter(el=>el.matches('[data-live-assistant="1"],.tool-call-group,.tool-card-row,.agent-activity-thinking')).pop();
  const group=ensureActivityGroup(inner,{live:true,collapsed:true,anchor,activityKey:_activityKeyForLiveTurn()});
  const body=group.querySelector('.tool-call-group-body');
  const toolName=_toolDisplayName(tc);
  const toolEventId=tid?`tool-${tid}`:`tool-${String(tc.name||'tool').replace(/[^a-z0-9_-]/gi,'_')}`;
  const toolDone=tc.done!==false;
  _appendActivityEvent(group,{
    id:toolEventId,
    kind:'tool',
    label:toolDone?`Tool finished: ${toolName}`:`Running tool: ${toolName}`,
    detail:tc.preview||tc.snippet||'',
    status:toolDone?(tc.is_error?'error':'done'):'waiting',
    ts:_activityNowSeconds(),
  });
  const waiting=body.querySelector('.agent-activity-status[data-activity-event-id="thinking-placeholder"]');
  if(waiting&&!toolDone){
    const labelEl=waiting.querySelector('.agent-activity-status-label');
    const detailEl=waiting.querySelector('.agent-activity-status-detail');
    if(labelEl) labelEl.textContent='Waiting on tool result';
    if(detailEl) detailEl.textContent=`${_activityProgressLabelForToolName(toolName)}: ${toolName}. Results will appear here.`;
  }
  // Update existing card in place (tool_complete after tool_start)
  if(tid){
    const existing=body.querySelector(`.tool-card-row[data-live-tid="${CSS.escape(tid)}"]`);
    if(existing){
      const replacement=buildToolCard(tc);
      replacement.dataset.liveTid=tid;
      existing.replaceWith(replacement);
      _syncToolCallGroupSummary(group);
      return;
    }
  }
  const row=buildToolCard(tc);
  if(tid) row.dataset.liveTid=tid;
  body.appendChild(row);
  _syncToolCallGroupSummary(group);
  if(typeof scrollIfPinned==='function') scrollIfPinned();
}

function clearLiveToolCards(){
  if(typeof _clearActivityElapsedTimer==='function') _clearActivityElapsedTimer();
  const inner=_assistantTurnBlocks($('liveAssistantTurn'));
  if(inner) inner.querySelectorAll('.tool-call-group[data-live-tool-call-group],.tool-card-row[data-live-tid]').forEach(el=>el.remove());
  // Reset the per-turn user expand intent so the next turn starts at the
  // default collapsed state (#1298).
  if(typeof _clearLiveActivityUserIntent==='function') _clearLiveActivityUserIntent();
  // Legacy #liveToolCards container cleanup — kept for safety in case any
  // leftover cards were inserted there before this refactor took effect.
  const container=$('liveToolCards');
  if(container){container.innerHTML='';container.style.display='none';}
}

// ── Edit + Regenerate ──

function editMessage(btn) {
  if(S.busy) return;
  const row = btn.closest('[data-msg-idx]');
  if(!row) return;
  const msgIdx = parseInt(row.dataset.msgIdx, 10);
  const originalText = row.dataset.rawText || '';
  const body = row.querySelector('.msg-body');
  if(!body || row.dataset.editing) return;
  row.dataset.editing = '1';

  // Replace msg-body with an editable textarea
  const ta = document.createElement('textarea');
  ta.className = 'msg-edit-area';
  ta.value = originalText;
  body.replaceWith(ta);
  // Resize after DOM insertion so scrollHeight is correct
  requestAnimationFrame(() => { autoResizeTextarea(ta); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); });
  ta.addEventListener('input', () => autoResizeTextarea(ta));

  // Action bar below the textarea
  const bar = document.createElement('div');
  bar.className = 'msg-edit-bar';
  bar.innerHTML = `<button class="msg-edit-send">Send edit</button><button class="msg-edit-cancel">Cancel</button>`;
  ta.after(bar);

  bar.querySelector('.msg-edit-send').onclick = async () => {
    const newText = ta.value.trim();
    if(!newText) return;
    await submitEdit(msgIdx, newText);
  };
  bar.querySelector('.msg-edit-cancel').onclick = () => cancelEdit(row, originalText, body);

  ta.addEventListener('keydown', e => {
    if(e.key==='Enter' && !e.shiftKey) { if(window._isImeEnter&&window._isImeEnter(e)) return; e.preventDefault(); bar.querySelector('.msg-edit-send').click(); }
    if(e.key==='Escape') { e.preventDefault(); cancelEdit(row, originalText, body); }
  });
}

function cancelEdit(row, originalText, originalBody) {
  delete row.dataset.editing;
  const ta = row.querySelector('.msg-edit-area');
  const bar = row.querySelector('.msg-edit-bar');
  if(ta) ta.replaceWith(originalBody);
  if(bar) bar.remove();
}

function autoResizeTextarea(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 300) + 'px';
}

async function submitEdit(msgIdx, newText) {
  if(!S.session || S.busy) return;
  // Truncate session at msgIdx (keep messages before the edited one)
  // then re-send the edited text
  try {
    await api('/api/session/truncate', {method:'POST', body:JSON.stringify({
      session_id: S.session.session_id,
      keep_count: msgIdx  // keep messages[0..msgIdx-1], discard from msgIdx onward
    })});
    S.messages = S.messages.slice(0, msgIdx);
    renderMessages();
    // Now send the edited message as a new chat
    $('msg').value = newText;
    await send();
  } catch(e) { setStatus(t('edit_failed') + e.message); }
}

async function regenerateResponse(btn) {
  if(!S.session || S.busy) return;
  // Find the last user message and re-run it
  // Remove the last assistant message first (truncate to before it)
  const row = btn.closest('[data-msg-idx]');
  if(!row) return;
  const assistantIdx = parseInt(row.dataset.msgIdx, 10);
  // Find the last user message text (one before this assistant message)
  let lastUserText = '';
  for(let i = assistantIdx - 1; i >= 0; i--) {
    const m = S.messages[i];
    if(m && m.role === 'user') { lastUserText = msgContent(m); break; }
  }
  if(!lastUserText) return;
  try {
    await api('/api/session/truncate', {method:'POST', body:JSON.stringify({
      session_id: S.session.session_id,
      keep_count: assistantIdx  // remove the assistant message
    })});
    S.messages = S.messages.slice(0, assistantIdx);
    renderMessages();
    $('msg').value = lastUserText;
    await send();
  } catch(e) { setStatus(t('regen_failed') + e.message); }
}

function postProcessRenderedMessages(container) {
  highlightCode(container);
  addCopyButtons(container);
  loadDiffInline(container);
  loadCsvInline(container);
  loadExcalidrawInline(container);
  loadPdfInline(container);
  loadHtmlInline(container);
  renderMermaidBlocks(container);
  renderKatexBlocks(container);
  initTreeViews(container);
}

function highlightCode(container) {
  // Apply Prism.js syntax highlighting only to *new* code blocks.
  // Previously every renderMessages() called Prism.highlightAllUnder() which
  // re-scanned and re-highlighted every <pre> in the container — expensive in
  // long sessions with dozens of code blocks.  Now we only touch blocks that
  // don't already have the data-highlighted marker.
  if(typeof Prism === 'undefined') return;
  const el = container || $('msgInner');
  if(!el) return;
  // Prefer per-element highlight (avoids the full DOM walk of highlightAllUnder)
  const blocks = el.querySelectorAll('pre code:not([data-highlighted])');
  if(blocks.length === 0) return;
  for(let i = 0; i < blocks.length; i++){
    const block = blocks[i];
    if(typeof Prism.highlightElement === 'function') Prism.highlightElement(block);
    block.dataset.highlighted = '1';
  }
}

// Lazy load js-yaml for YAML tree view support
let _jsyamlLoading=false;
function _loadJsyamlThen(cb){
  if(typeof jsyaml!=='undefined'){ cb(); return; }
  if(_jsyamlLoading){ setTimeout(()=>_loadJsyamlThen(cb),100); return; }
  _jsyamlLoading=true;
  const s=document.createElement('script');
  s.src='static/vendor/js-yaml/4.1.0/js-yaml.min.js';
  s.integrity='sha384-+pxiN6T7yvpryuJmE1gM9PX7yQit15auDb+ZwwvJOd/4be2Cie5/IuVXgQb/S9du';
  s.crossOrigin='anonymous';
  s.onload=()=>{ _jsyamlLoading=false; cb(); };
  s.onerror=()=>{ _jsyamlLoading=false; }; // CDN blocked, fall back to raw
  document.head.appendChild(s);
}

function initTreeViews(container){
  const root=container||document;
  root.querySelectorAll('.code-tree-wrap:not([data-tree-init])').forEach(wrap=>{
    const rawText=wrap.dataset.raw;
    const lang=wrap.dataset.lang;
    let parsed=null;
    let parseFailed=false;
    // Try JSON parse
    try{ parsed=JSON.parse(rawText); }catch(e){ parseFailed=(lang==='json'); }
    // YAML: lazy-load js-yaml if needed
    if(!parsed && lang==='yaml'){
      if(typeof jsyaml!=='undefined'){
        try{ parsed=jsyaml.load(rawText); }catch(e){ parseFailed=true; }
      }else{
        // Defer: remove init marker so we retry after load.
        // Note: if CDN load fails, s.onerror does NOT call back —
        // the wrap stays un-initialised (raw view only), which is safe.
        wrap.removeAttribute('data-tree-init');
        _loadJsyamlThen(initTreeViews);
        return;
      }
    }
    // Mark as initialised only after we've committed to a render decision
    wrap.setAttribute('data-tree-init','1');
    if(!parsed || typeof parsed!=='object'){
      if(parseFailed){
        const hint=wrap.querySelector('.tree-raw-view');
        if(hint&&!hint.querySelector('.tree-parse-note')){
          const note=document.createElement('div');
          note.className='tree-parse-note';
          note.textContent=t('parse_failed_note')||'parse failed';
          hint.parentNode.insertBefore(note,hint.nextSibling);
        }
      }
      return; // leave as raw view
    }
    const lineCount=rawText.split('\n').length;
    // Default to raw for short blocks (<10 lines), tree for longer
    const showTree=lineCount>=10;
    // Build tree DOM
    const treeDiv=document.createElement('div');
    treeDiv.className='tree-view'+(showTree?'':' tree-hidden');
    treeDiv.appendChild(_buildTreeDOM(parsed, 0));
    // Toggle button in header
    const header=wrap.querySelector('.pre-header');
    if(header){
      const toggle=document.createElement('button');
      toggle.className='tree-toggle-btn';
      toggle.textContent=showTree?t('raw_view'):t('tree_view');
      toggle.onclick=(e)=>{
        e.stopPropagation();
        const isTreeHidden=treeDiv.classList.contains('tree-hidden');
        treeDiv.classList.toggle('tree-hidden',!isTreeHidden);
        const rawPre=wrap.querySelector('.tree-raw-view');
        if(rawPre) rawPre.style.display=isTreeHidden?'none':'';
        toggle.textContent=isTreeHidden?t('raw_view'):t('tree_view');
      };
      header.style.display='flex';
      header.style.justifyContent='space-between';
      header.style.alignItems='center';
      header.appendChild(toggle);
    }
    if(!showTree){
      const rawPre=wrap.querySelector('.tree-raw-view');
      if(rawPre) rawPre.style.display='';
    } else {
      const rawPre=wrap.querySelector('.tree-raw-view');
      if(rawPre) rawPre.style.display='none';
    }
    wrap.appendChild(treeDiv);
  });
}

function _buildTreeDOM(val, depth){
  const el=document.createElement('div');
  el.className='tree-node';
  if(val===null){ el.innerHTML=`<span class="tree-val tree-null">null</span>`; return el; }
  if(typeof val==='boolean'){ el.innerHTML=`<span class="tree-val tree-bool">${val}</span>`; return el; }
  if(typeof val==='number'){ el.innerHTML=`<span class="tree-val tree-num">${val}</span>`; return el; }
  if(typeof val==='string'){ el.innerHTML=`<span class="tree-val tree-str">&quot;${esc(val)}&quot;</span>`; return el; }
  if(Array.isArray(val)){
    el.classList.add('tree-array');
    const collapsed=depth>=2;
    const header=document.createElement('span');
    header.className='tree-collapsible';
    header.innerHTML=(collapsed?'▸ ': '▾ ')+`<span class="tree-bracket">[</span><span class="tree-count">${val.length}</span><span class="tree-bracket">]</span>`;
    const body=document.createElement('div');
    body.className='tree-children'+(collapsed?' tree-collapsed':'');
    val.forEach((item,i)=>{
      const child=document.createElement('div');
      child.className='tree-item';
      child.appendChild(_buildTreeDOM(item, depth+1));
      if(i<val.length-1) child.innerHTML+='<span class="tree-comma">,</span>';
      body.appendChild(child);
    });
    el.appendChild(header);
    el.appendChild(body);
    header.onclick=(()=>{const c=body.classList.contains('tree-collapsed'); body.classList.toggle('tree-collapsed'); header.innerHTML=(c?'▾ ':'▸ ')+`<span class="tree-bracket">[</span><span class="tree-count">${val.length}</span><span class="tree-bracket">]</span>`;});
    return el;
  }
  if(typeof val==='object'){
    el.classList.add('tree-object');
    const keys=Object.keys(val);
    const collapsed=depth>=2;
    const header=document.createElement('span');
    header.className='tree-collapsible';
    header.innerHTML=(collapsed?'▸ ': '▾ ')+`<span class="tree-bracket">{</span><span class="tree-count">${keys.length}</span><span class="tree-bracket">}</span>`;
    const body=document.createElement('div');
    body.className='tree-children'+(collapsed?' tree-collapsed':'');
    keys.forEach((key,i)=>{
      const child=document.createElement('div');
      child.className='tree-item';
      child.innerHTML=`<span class="tree-key">&quot;${esc(key)}&quot;</span><span class="tree-colon">: </span>`;
      child.appendChild(_buildTreeDOM(val[key], depth+1));
      if(i<keys.length-1) child.innerHTML+='<span class="tree-comma">,</span>';
      body.appendChild(child);
    });
    el.appendChild(header);
    el.appendChild(body);
    header.onclick=(()=>{const c=body.classList.contains('tree-collapsed'); body.classList.toggle('tree-collapsed'); header.innerHTML=(c?'▾ ':'▸ ')+`<span class="tree-bracket">{</span><span class="tree-count">${keys.length}</span><span class="tree-bracket">}</span>`;});
    return el;
  }
  el.innerHTML=`<span class="tree-val">${esc(String(val))}</span>`;
  return el;
}

function addCopyButtons(container){
  const el=container||$('msgInner');
  if(!el) return;
  el.querySelectorAll('pre > code').forEach(codeEl=>{
    const pre=codeEl.parentElement;
    const header=pre.previousElementSibling;
    if(pre.querySelector('.code-copy-btn')||(header&&header.classList.contains('pre-header')&&header.querySelector('.code-copy-btn'))) return;
    const btn=document.createElement('button');
    btn.className='code-copy-btn';
    btn.textContent=t('copy');
    btn.onclick=(e)=>{
      e.stopPropagation();
      _copyText(codeEl.textContent).then(()=>{
        btn.textContent=t('copied');
        setTimeout(()=>{btn.textContent=t('copy');},1500);
      }).catch(()=>{btn.textContent=t('copy_failed');setTimeout(()=>{btn.textContent=t('copy');},1500);});
    };
    if(header&&header.classList.contains('pre-header')){
      header.style.display='flex';
      header.style.justifyContent='space-between';
      header.style.alignItems='center';
      header.appendChild(btn);
    }else{
      pre.style.position='relative';
      btn.style.cssText='position:absolute;top:6px;right:6px;';
      pre.appendChild(btn);
    }
  });
}

let _mermaidLoading=false;
let _mermaidReady=false;

function loadDiffInline(container){
  const DIFF_MAX_SIZE=512*1024; // 512 KB cap for inline diff rendering
  const root=container||document;
  root.querySelectorAll('.diff-inline-load:not([data-loaded])').forEach(el=>{
    el.setAttribute('data-loaded','1');
    const path=el.dataset.path;
    fetch('api/media?path='+encodeURIComponent(path))
      .then(r=>{if(!r.ok) throw new Error(r.status);return r.text();})
      .then(text=>{
        if(text.length>DIFF_MAX_SIZE){
          el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('diff_too_large')}</span></div>`;
          return;
        }
        const lines=text.split('\n').map(line=>{
          const e=esc(line);
          if(e.startsWith('@@')) return `<span class="diff-line diff-hunk">${e}</span>`;
          if(e.startsWith('+')) return `<span class="diff-line diff-plus">${e}</span>`;
          if(e.startsWith('-')) return `<span class="diff-line diff-minus">${e}</span>`;
          return `<span class="diff-line">${e}</span>`;
        }).join('\n');
        el.outerHTML=`<div class="diff-inline"><div class="pre-header">${esc(path.split('/').pop())}</div><pre class="diff-block"><code>${lines}</code></pre></div>`;
      })
      .catch(()=>{
        el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('diff_error')}</span></div>`;
      });
  });
}

function loadCsvInline(container){
  const CSV_MAX_SIZE=256*1024; // 256 KB cap for inline CSV rendering
  const root=container||document;
  root.querySelectorAll('.csv-inline-load:not([data-loaded])').forEach(el=>{
    el.setAttribute('data-loaded','1');
    const path=el.dataset.path;
    fetch('api/media?path='+encodeURIComponent(path))
      .then(r=>{if(!r.ok) throw new Error(r.status);return r.text();})
      .then(text=>{
        if(text.length>CSV_MAX_SIZE){
          el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('csv_too_large')}</span></div>`;
          return;
        }
        const rows=text.replace(/\r\n/g,'\n').replace(/\r/g,'\n').split('\n').filter(r=>r.trim());
        if(rows.length<2){
          el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('csv_no_data')}</span></div>`;
          return;
        }
        // Auto-detect separator (comma, semicolon, tab)
        // Heuristic: uses the first separator found in the header row. Edge case:
        // quoted fields containing commas without non-quoted commas in the header
        // could cause misdetection — acceptable trade-off for a preview renderer.
        const firstLine=rows[0];
        const separators=[',',';','\t'];
        let sep=separators.find(s=>firstLine.includes(s))||',';
        const headers=rows[0].split(sep).map(c=>c.trim().replace(/^["']|["']$/g,''));
        const bodyRows=rows.slice(1).map(r=>'<tr>'+r.split(sep).map(c=>`<td>${esc(c.trim().replace(/^["']|["']$/g,''))}</td>`).join('')+'</tr>').join('');
        const headerRow=headers.map(h=>`<th>${esc(h)}</th>`).join('');
        el.outerHTML=`<div class="csv-table-wrap"><div class="pre-header">${esc(path.split('/').pop())} <span style="opacity:.5;font-size:11px">${t('csv_header_note')}</span></div><table class="csv-table"><thead><tr>${headerRow}</tr></thead><tbody>${bodyRows}</tbody></table></div>`;
      })
      .catch(()=>{
        el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('csv_error')}</span></div>`;
      });
  });
}

function loadExcalidrawInline(container){
  const EXCALIDRAW_MAX_SIZE=512*1024; // 512 KB cap
  const root=container||document;
  root.querySelectorAll('.excalidraw-inline-load:not([data-loaded])').forEach(el=>{
    el.setAttribute('data-loaded','1');
    const path=el.dataset.path;
    fetch('api/media?path='+encodeURIComponent(path))
      .then(r=>{if(!r.ok) throw new Error(r.status);return r.text();})
      .then(text=>{
        if(text.length>EXCALIDRAW_MAX_SIZE){
          el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('excalidraw_too_large')}</span></div>`;
          return;
        }
        // Validate it looks like Excalidraw JSON
        let data;
        try{data=JSON.parse(text);}catch(e){
          el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('excalidraw_invalid')}</span></div>`;
          return;
        }
        if(!data.type||data.type!=='excalidraw'){
          el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('excalidraw_invalid')}</span></div>`;
          return;
        }
        const fname=esc(path.split('/').pop());
        const downloadUrl='api/media?path='+encodeURIComponent(path)+'&download=1';
        el.outerHTML=`<div class="excalidraw-embed-wrap" title="${t('excalidraw_simplified')}">
  <div class="msg-artifact-header">
    <span class="msg-media-label">${t('excalidraw_label')}</span>
    <a class="excalidraw-open-link" href="${downloadUrl}" download="${fname}">${t('excalidraw_download')} ${fname}</a>
  </div>
  <div class="excalidraw-canvas" data-excalidraw='${esc(text)}'></div>
</div>`;
        // Lazy-init Excalidraw render after DOM insertion
        requestAnimationFrame(()=>_renderExcalidrawCanvases());
      })
      .catch(()=>{
        el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('excalidraw_error')}</span></div>`;
      });
  });
}

let _excalidrawScriptLoaded=false;
function _renderExcalidrawCanvases(){
  document.querySelectorAll('.excalidraw-canvas:not([data-rendered])').forEach(el=>{
    el.setAttribute('data-rendered','1');
    const dataStr=el.getAttribute('data-excalidraw');
    if(!dataStr) return;
    // Render a simple SVG preview using the Excalidraw elements
    try{
      const data=JSON.parse(dataStr);
      const elements=data.elements||[];
      if(!elements.length){el.innerHTML=`<div class="excalidraw-empty">${t('excalidraw_empty')}</div>`;return;}
      // Calculate bounds
      let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
      elements.forEach(el=>{
        const b=[el.x||0,el.y||0,(el.x||0)+(el.width||0),(el.y||0)+(el.height||0)];
        minX=Math.min(minX,b[0]);minY=Math.min(minY,b[1]);
        maxX=Math.max(maxX,b[2]);maxY=Math.max(maxY,b[3]);
      });
      const pad=20;minX-=pad;minY-=pad;maxX+=pad;maxY+=pad;
      const w=Math.max(maxX-minX,200);const h=Math.max(maxY-minY,150);
      // SVG attributes are rendered via innerHTML below, so attacker-controlled
      // values from JSON (e.g. strokeColor='red"/><script>...') would break out
      // of the attribute. Escape strings; coerce numerics.
      const _sa=v=>String(v==null?'':v).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const _num=(v,fb)=>{const n=Number(v);return Number.isFinite(n)?n:fb;};
      const svgParts=[`<svg xmlns="http://www.w3.org/2000/svg" viewBox="${_num(minX,0)} ${_num(minY,0)} ${_num(w,200)} ${_num(h,150)}" class="excalidraw-svg">`];
      elements.forEach(el=>{
        const stroke=_sa(el.strokeColor||'#1e1e1e');
        const fill=_sa(el.backgroundColor||'transparent');
        const sw=_num(el.strokeWidth,2);
        const x=_num(el.x,0),y=_num(el.y,0),w=_num(el.width,0),h=_num(el.height,0);
        if(el.type==='rectangle'){
          svgParts.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" stroke="${stroke}" stroke-width="${sw}" fill="${fill}" rx="${el.roundness?.type===3?8:0}"/>`);
        }else if(el.type==='diamond'){
          const cx=x+w/2,cy=y+h/2;
          svgParts.push(`<polygon points="${cx},${y} ${x+w},${cy} ${cx},${y+h} ${x},${cy}" stroke="${stroke}" stroke-width="${sw}" fill="${fill}"/>`);
        }else if(el.type==='ellipse'){
          svgParts.push(`<ellipse cx="${x+w/2}" cy="${y+h/2}" rx="${w/2}" ry="${h/2}" stroke="${stroke}" stroke-width="${sw}" fill="${fill}"/>`);
        }else if(el.type==='line'){
          const pts=(el.points||[]).filter(p=>Array.isArray(p)&&p.length>=2);
          if(!pts.length) return;
          let d=`M ${_num(x+_num(pts[0][0],0),0)} ${_num(y+_num(pts[0][1],0),0)}`;
          for(let i=1;i<pts.length;i++) d+=` L ${_num(x+_num(pts[i][0],0),0)} ${_num(y+_num(pts[i][1],0),0)}`;
          svgParts.push(`<path d="${d}" stroke="${stroke}" stroke-width="${sw}" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`);
        }else if(el.type==='arrow'){
          const pts=(el.points||[]).filter(p=>Array.isArray(p)&&p.length>=2);
          if(!pts.length) return;
          let d=`M ${_num(x+_num(pts[0][0],0),0)} ${_num(y+_num(pts[0][1],0),0)}`;
          for(let i=1;i<pts.length;i++) d+=` L ${_num(x+_num(pts[i][0],0),0)} ${_num(y+_num(pts[i][1],0),0)}`;
          svgParts.push(`<path d="${d}" stroke="${stroke}" stroke-width="${sw}" fill="none" stroke-linecap="round" stroke-linejoin="round" marker-end="url(#arrowhead)"/>`);
        }else if(el.type==='text'){
          const fontSize=_num(el.fontSize,20);
          const txt=String(el.text==null?'':el.text);
          const lines=txt.split('\n');
          lines.forEach((line,i)=>{
            svgParts.push(`<text x="${x}" y="${y+i*fontSize*1.2+fontSize}" fill="${stroke}" font-size="${fontSize}" font-family="Virgil, Segoe UI Emoji, sans-serif">${esc(line)}</text>`);
          });
        }else if(el.type==='draw'){
          const pts=(el.points||[]).filter(p=>Array.isArray(p)&&p.length>=2);
          if(pts.length>1){
            let d=`M ${_num(x+_num(pts[0][0],0),0)} ${_num(y+_num(pts[0][1],0),0)}`;
            for(let i=1;i<pts.length;i++) d+=` L ${_num(x+_num(pts[i][0],0),0)} ${_num(y+_num(pts[i][1],0),0)}`;
            svgParts.push(`<path d="${d}" stroke="${stroke}" stroke-width="${sw}" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`);
          }
        }
        // Unknown element types (e.g. image, frame, group, freedraw) are
        // silently skipped to avoid breaking the render. This is a simplified
        // SVG preview, not a pixel-identical Excalidraw canvas reproduction.
      });
      // Arrow marker definition
      svgParts.unshift(`<defs><marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto"><polygon points="0 0, 10 3.5, 0 7" fill="#1e1e1e"/></marker></defs>`);
      svgParts.push('</svg>');
      el.innerHTML=svgParts.join('');
    }catch(e){
      el.innerHTML=`<div class="excalidraw-empty">${t('excalidraw_render_error')}</div>`;
    }
  });
}

// ── PDF inline preview (first page) ────────────────────────────────────────
// NOTE: PDF.js is loaded from CDN (jsdelivr). Offline/air-gapped deployments
// will not get inline previews; the 15 s fallback timeout degrades to a
// download link in that case. The 4 MB size cap is checked client-side after
// the full buffer is received — ideally the server would enforce it before
// streaming (out of scope for this client-side PR).
let _pdfjsReady=false, _pdfjsLoading=false;
function loadPdfInline(container){
  const PDF_MAX_SIZE=4*1024*1024; // 4 MB cap for inline PDF preview
  const root=container||document;
  root.querySelectorAll('.pdf-preview-load:not([data-loaded])').forEach(el=>{
    el.setAttribute('data-loaded','1');
    const path=el.dataset.path;
    const fname=path.split('/').pop()||path;
    const loadPdf=(pdfjsLib)=>{
      fetch('api/media?path='+encodeURIComponent(path))
        .then(r=>{if(!r.ok) throw new Error(r.status); return r.arrayBuffer();})
        .then(buf=>{
          if(buf.byteLength>PDF_MAX_SIZE){
            el.outerHTML=`<div class="pdf-preview-fallback"><a class="msg-media-link" href="api/media?path=${encodeURIComponent(path)}&download=1" download="${esc(fname)}">📎 ${esc(fname)}</a><br><span style="color:var(--muted);font-size:12px">${t('pdf_too_large')}</span></div>`;
            return;
          }
          return pdfjsLib.getDocument({data:buf, isEvalSupported:false}).promise;
        })
        .then(pdf=>{
          if(!pdf) return;
          pdf.getPage(1).then(page=>{
            const canvas=document.createElement('canvas');
            const scale=1.5;
            const viewport=page.getViewport({scale});
            canvas.width=viewport.width;
            canvas.height=viewport.height;
            canvas.className='pdf-preview-canvas';
            page.render({canvasContext:canvas.getContext('2d'),viewport}).promise.then(()=>{
              // Canvas bitmap is runtime state, not part of HTML serialization.
              // Attach the canvas as a DOM node — interpolating its serialized
              // form into a template string parses back as an empty canvas.
              const dlUrl='api/media?path='+encodeURIComponent(path)+'&download=1';
              const wrap=document.createElement('div');
              wrap.className='pdf-preview-wrap';
              wrap.innerHTML=`<div class="pdf-preview-header"><span>📄 ${esc(fname)}</span><a href="${dlUrl}" download="${esc(fname)}" class="pdf-download-link">${t('pdf_download')} ↓</a></div><div class="pdf-preview-body"></div>`;
              wrap.querySelector('.pdf-preview-body').appendChild(canvas);
              el.replaceWith(wrap);
            });
          });
        })
        .catch(()=>{
          const dlUrl='api/media?path='+encodeURIComponent(path)+'&download=1';
          el.outerHTML=`<div class="pdf-preview-fallback"><a class="msg-media-link" href="${dlUrl}" download="${esc(fname)}">📎 ${esc(fname)}</a><br><span style="color:var(--muted);font-size:12px">${t('pdf_error')}</span></div>`;
        });
    };
    if(_pdfjsReady){
      loadPdf(window._pdfjsLib);
    } else if(!_pdfjsLoading){
      _pdfjsLoading=true;
      const _pdfSrc='https://cdn.jsdelivr.net/npm/pdfjs-dist@4.9.155/build/pdf.min.mjs';
      const _pdfWorker='https://cdn.jsdelivr.net/npm/pdfjs-dist@4.9.155/build/pdf.worker.min.mjs';
      const _pdfBlob=new Blob([`import*as p from'${_pdfSrc}';p.GlobalWorkerOptions.workerSrc='${_pdfWorker}';window._pdfjsLib=p;window._pdfjsReady=true;window.dispatchEvent(new Event('pdfjs-ready'));`],{type:'application/javascript'});
      const s=document.createElement('script');
      s.type='module';
      const _pdfBlobUrl=URL.createObjectURL(_pdfBlob);
      s.src=_pdfBlobUrl;
      s.onload=()=>URL.revokeObjectURL(_pdfBlobUrl);
      document.head.appendChild(s);
      window.addEventListener('pdfjs-ready',()=>{ _pdfjsReady=true; loadPdf(window._pdfjsLib); },{once:true});
      setTimeout(()=>{
        if(!_pdfjsReady){
          const dlUrl='api/media?path='+encodeURIComponent(path)+'&download=1';
          if(el.parentNode){
            el.outerHTML=`<div class="pdf-preview-fallback"><a class="msg-media-link" href="${dlUrl}" download="${esc(fname)}">📎 ${esc(fname)}</a><br><span style="color:var(--muted);font-size:12px">${t('pdf_error')}</span></div>`;
          }
        }
      },15000);
    } else {
      window.addEventListener('pdfjs-ready',()=>{ loadPdf(window._pdfjsLib); },{once:true});
    }
  });
}

// ── HTML inline preview (sandboxed iframe) ─────────────────────────────────
function loadHtmlInline(container){
  const HTML_MAX_SIZE=256*1024; // 256 KB cap for inline HTML preview
  const root=container||document;
  root.querySelectorAll('.html-preview-load:not([data-loaded])').forEach(el=>{
    el.setAttribute('data-loaded','1');
    const path=el.dataset.path;
    const fname=path.split('/').pop()||path;
    fetch('api/media?path='+encodeURIComponent(path))
      .then(r=>{if(!r.ok) throw new Error(r.status); return r.text();})
      .then(html=>{
        if(html.length>HTML_MAX_SIZE){
          const openUrl='api/media?path='+encodeURIComponent(path)+'&inline=1';
          el.outerHTML=`<div class="html-preview-fallback"><a class="msg-media-link" href="${openUrl}" target="_blank" rel="noopener">📎 ${esc(fname)}</a><br><span style="color:var(--muted);font-size:12px">${t('html_too_large')}</span></div>`;
          return;
        }
        const openUrl='api/media?path='+encodeURIComponent(path)+'&inline=1';
        const safeHtml=html.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        el.outerHTML=`<div class="html-preview-wrap"><div class="html-preview-header"><span>${t('html_sandbox_label')}</span><a href="${openUrl}" target="_blank" rel="noopener" class="html-open-link">${t('html_open_full')} ↗</a></div><iframe srcdoc="${safeHtml}" sandbox="allow-scripts" class="html-preview-iframe" loading="lazy"></iframe></div>`;
      })
      .catch(()=>{
        const dlUrl='api/media?path='+encodeURIComponent(path)+'&download=1';
        el.outerHTML=`<div class="html-preview-fallback"><a class="msg-media-link" href="${dlUrl}" download="${esc(fname)}">📎 ${esc(fname)}</a><br><span style="color:var(--muted);font-size:12px">${t('html_error')}</span></div>`;
      });
  });
}

function renderMermaidBlocks(container){
  const root=container||document;
  const blocks=root.querySelectorAll('.mermaid-block:not([data-rendered])');
  if(!blocks.length) return;
  if(!_mermaidReady){
    if(!_mermaidLoading){
      _mermaidLoading=true;
      const script=document.createElement('script');
      script.src='https://cdn.jsdelivr.net/npm/mermaid@10.9.3/dist/mermaid.min.js';
      script.integrity='sha384-R63zfMfSwJF4xCR11wXii+QUsbiBIdiDzDbtxia72oGWfkT7WHJfmD/I/eeHPJyT';
      script.crossOrigin='anonymous';
      script.onload=()=>{
        if(typeof mermaid!=='undefined'){
          mermaid.initialize({startOnLoad:false,theme:document.documentElement.classList.contains('dark')?'dark':'default',themeVariables:{
            fontFamily:'inherit',fontSize:'14px',
            primaryColor:'#4a6fa5',primaryTextColor:'#e2e8f0',lineColor:'#718096',
            secondaryColor:'#2d3748',tertiaryColor:'#1a202c',primaryBorderColor:'#4a5568',
          }});
          _mermaidReady=true;
          renderMermaidBlocks();
        }
      };
      document.head.appendChild(script);
    }
    return;
  }
  blocks.forEach(async(block)=>{
    block.dataset.rendered='true';
    const code=block.textContent;
    const id=block.dataset.mermaidId||('m-'+Math.random().toString(36).slice(2));
    try{
      const {svg}=await mermaid.render(id,code);
      const tmp=document.getElementById('d'+id);
      if(tmp) tmp.remove();
      block.innerHTML=svg;
      block.classList.add('mermaid-rendered');
    }catch(e){
      const tmp=document.getElementById('d'+id);
      if(tmp) tmp.remove();
      // Fall back to showing as a code block. Remove the mermaid marker so a
      // later render pass cannot retry this already-failed block.
      block.classList.remove('mermaid-block');
      block.classList.add('prewrap');
      block.innerHTML=`<div class="pre-header">mermaid</div><pre><code>${esc(code)}</code></pre>`;
    }
  });
}

let _katexLoading=false;
let _katexReady=false;

function _isStreamingEquationPending(el,root){
  const tagName=(el&&el.tagName||'').toLowerCase();
  if(tagName!=='equation-block'&&tagName!=='equation-inline') return false;
  // streaming-markdown fills custom equation elements while the parser owns the
  // open node. If the equation is currently the last descendant of the live
  // assistant body, we cannot tell whether more TeX is still coming. Skip it
  // during live debounce passes so a partial source is not permanently marked
  // data-rendered before the final parser_end flush.
  let node=el;
  while(node&&node!==root){
    if(node.nextSibling) return false;
    node=node.parentNode;
  }
  return Boolean(node===root);
}

function renderKatexBlocks(container,options){
  const root=container||document;
  const streaming=Boolean(options&&options.streaming);
  const blocks=root.querySelectorAll(
    '.katex-block:not([data-rendered]),.katex-inline:not([data-rendered]),'+
    'equation-block:not([data-rendered]),equation-inline:not([data-rendered])'
  );
  if(!blocks.length) return;
  if(!_katexReady){
    if(!_katexLoading){
      _katexLoading=true;
      const script=document.createElement('script');
      script.src='static/vendor/katex/0.16.22/katex.min.js';
      script.integrity='sha384-cMkvdD8LoxVzGF/RPUKAcvmm49FQ0oxwDF3BGKtDXcEc+T1b2N+teh/OJfpU0jr6';
      script.crossOrigin='anonymous';
      script.onload=()=>{
        if(typeof katex!=='undefined'){
          _katexReady=true;
          renderKatexBlocks();
        }
      };
      document.head.appendChild(script);
    }
    return;
  }
  blocks.forEach(el=>{
    if(streaming&&_isStreamingEquationPending(el,root)) return;
    el.dataset.rendered='true';
    const src=el.textContent||'';
    const tagName=(el.tagName||'').toLowerCase();
    const displayMode=el.dataset.katex==='display'||tagName==='equation-block';
    try{
      katex.render(src,el,{
        displayMode,
        throwOnError:false,
        trust:false,
        strict:'ignore',
      });
    }catch(e){
      // Leave as raw text in a code span on failure
      el.outerHTML=`<code>${esc(src)}</code>`;
    }
  });
}

function _thinkingMarkup(text=''){
  const clean=_sanitizeThinkingDisplayText(text);
  const openClass=isSimplifiedToolCalling()?'':' open';
  return (clean&&String(clean).trim())
    ? `<div class="thinking-card${openClass}"><div class="thinking-card-header" onclick="this.parentElement.classList.toggle('open')"><span class="thinking-card-icon">${li('lightbulb',14)}</span><span class="thinking-card-label">${t('thinking')}</span><span class="thinking-card-toggle">${li('chevron-right',12)}</span></div><div class="thinking-card-body"><pre>${esc(String(clean).trim())}</pre></div></div>`
    : `<div class="thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>`;
}
function _renderThinkingInto(row,text=''){
  if(!row) return;
  const clean=_sanitizeThinkingDisplayText(text);
  if(!clean){
    row.innerHTML=_thinkingMarkup(text);
    return;
  }
  const pre=row.querySelector('.thinking-card-body pre');
  if(pre){
    pre.textContent=clean;
    return;
  }
  row.innerHTML=_thinkingMarkup(text);
}
function finalizeThinkingCard(){
  // Guard: only finalize thinking card if we're looking at the session that started it.
  // Without this check, switching tabs while a stream is running causes finalizeThinkingCard
  // to remove/modify the thinking card DOM of the wrong session — the card belongs to the
  // stream that started it, not the session currently displayed.
  const _guardTurn = $('liveAssistantTurn');
  if(_guardTurn && S.session && _guardTurn.dataset.sessionId !== S.session.session_id) return;
  if(!isSimplifiedToolCalling()){
    const row=$('thinkingRow');
    if(!row) return;
    // If the row is still just a spinner (no thinking content rendered),
    // remove it entirely — it's the initial waiting dots.
    const hasContent=row.querySelector('.thinking-card') || row.classList.contains('thinking-card-row');
    if(!hasContent && row.getAttribute('data-thinking-active')==='1'){
      row.remove();
      return;
    }
    // If the user was watching (scroll pinned = at bottom), scroll the thinking
    // card back to the top so the completed response is visible underneath without
    // the thinking content blocking it. If they scrolled up to read history,
    // leave their scroll position intact.
    if(_scrollPinned){
      const body=row&&row.querySelector('.thinking-card-body');
      if(body) body.scrollTop=0;
    }
    row.removeAttribute('id');
    row.removeAttribute('data-thinking-active');
    return;
  }
  const turn=$('liveAssistantTurn');
  const group=turn&&turn.querySelector('.tool-call-group[data-live-tool-call-group="1"]');
  if(group){
    // Respect the user's explicit expand intent (#1298) — only force-collapse
    // when the user has not manually expanded this turn's activity group, or
    // has manually collapsed it. Otherwise the panel snaps shut whenever new
    // activity arrives, even mid-read.
    if(_liveActivityUserExpanded !== true && !(window._activityFeedExpandedDefault === true && _liveActivityUserExpanded !== false)){
      group.classList.add('tool-call-group-collapsed');
      const summary=group.querySelector('.tool-call-group-summary');
      if(summary) summary.setAttribute('aria-expanded','false');
    }
    const active=turn.querySelector('.agent-activity-thinking[data-thinking-active="1"]');
    if(active) active.removeAttribute('data-thinking-active');
    _syncToolCallGroupSummary(group);
  }
}
function appendThinking(text='', options){
  // Guard: ignore if session was switched during an async SSE stream.
  // The old stream's reasoning events can still fire after switch;
  // without this check they would pollute the new session's DOM.
  const allowPendingPlaceholder=!!(options&&options.pending===true);
  if(!S.session||(!S.activeStreamId&&!allowPendingPlaceholder)) return;
  $('emptyState').style.display='none';
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    if(S.session) turn.dataset.sessionId=S.session.session_id;  // see #1366
    $('msgInner').appendChild(turn);
  }
  const blocks=_assistantTurnBlocks(turn);
  if(!blocks) return;
  if(!isSimplifiedToolCalling()){
    let row=$('thinkingRow');
    if(!row){
      row=document.createElement('div');
      row.className='assistant-segment';
      row.id='thinkingRow';
      row.setAttribute('data-thinking-active','1');
      // Insert after whichever comes last: a live assistant segment or a tool card.
      // This mirrors appendLiveToolCard's anchor logic so thinking always appears
      // in the right position in the interleaved sequence.
      // Also skip #toolRunningRow (dots) — thinking should go before dots, not after.
      const allChildren=Array.from(blocks.children);
      const anchor=allChildren.filter(el=>
        el.id!=='toolRunningRow' &&
        el.matches('[data-live-assistant="1"],.tool-card-row')
      ).pop();
      if(anchor) anchor.insertAdjacentElement('afterend', row);
      else blocks.appendChild(row);
    }
    const clean=_sanitizeThinkingDisplayText(text);
    const hasClean=!!String(clean||'').trim();
    row.className=hasClean?'assistant-segment thinking-card-row':'assistant-segment';
    _renderThinkingInto(row,text);
    scrollIfPinned();
    // Auto-scroll the thinking card body to bottom if the user is watching
    // (scroll pinned). If the user scrolled up to read history, leave it alone.
    if(_scrollPinned){
      const body=row&&row.querySelector('.thinking-card-body');
      if(body) body.scrollTop=body.scrollHeight;
    }
    return;
  }
  const thinkingText=String(text||'').trim()||'Thinking…';
  const cleanThinking=_sanitizeThinkingDisplayText(thinkingText);
  const allChildren=Array.from(blocks.children);
  const anchor=allChildren.filter(el=>
    el.id!=='toolRunningRow' &&
    el.matches('[data-live-assistant="1"],.tool-call-group,.tool-card-row')
  ).pop();
  const group=ensureActivityGroup(blocks,{live:true,collapsed:true,anchor,activityKey:_activityKeyForLiveTurn()});
  const body=group&&group.querySelector('.tool-call-group-body');
  if(!body) return;
  if(!cleanThinking||cleanThinking==='Thinking…'){
    const hasRunningTool=!!body.querySelector('.tool-card.tool-card-running');
    const hasToolCard=!!body.querySelector('.tool-card-row');
    let label;
    let detail;
    if(!S.activeStreamId && options && options.pending){
      label='Starting agent';
      detail='Creating the stream and sending your message…';
    }else if(hasRunningTool){
      label='Waiting on tool result';
      detail=_activityWaitingDetail(group,label);
    }else if(hasToolCard){
      label='Waiting on model';
      detail=_activityWaitingDetail(group,label);
    }else{
      label='Waiting for first model token';
      detail='Stream connected; no model output has arrived yet.';
    }
    _appendActivityEvent(group,{id:'thinking-placeholder',kind:'waiting',label,detail,status:'waiting',ts:_activityNowSeconds()});
    const active=body.querySelector('.agent-activity-thinking[data-thinking-active="1"]');
    if(active) active.removeAttribute('data-thinking-active');
    _syncToolCallGroupSummary(group);
    scrollIfPinned();
    return;
  }
  const placeholder=body.querySelector('.agent-activity-status[data-activity-event-id="thinking-placeholder"]');
  if(placeholder) placeholder.remove();
  let row=body.querySelector('.agent-activity-thinking[data-thinking-active="1"]');
  if(!row){
    const thinkingCards=Array.from(body.querySelectorAll('.agent-activity-thinking'));
    row=thinkingCards.pop()||null;
    if(row) row.setAttribute('data-thinking-active','1');
  }
  if(!row){
    row=_thinkingActivityNode(thinkingText, false);
    row.setAttribute('data-thinking-active','1');
    body.appendChild(row);
  }else{
    _renderThinkingInto(row,thinkingText);
  }
  _activityMarkObserved(group);
  _syncToolCallGroupSummary(group);
  scrollIfPinned();
  if(_scrollPinned){
    const body=row&&row.querySelector('.thinking-card-body');
    if(body) body.scrollTop=body.scrollHeight;
  }
}
function updateThinking(text=''){appendThinking(text);}
function removeThinking(){
  if(!isSimplifiedToolCalling()){
    const el=$('thinkingRow');
    if(el) el.remove();
    const turn=$('liveAssistantTurn');
    const blocks=_assistantTurnBlocks(turn);
    if(turn&&blocks&&!blocks.children.length) turn.remove();
    return;
  }
  const turn=$('liveAssistantTurn');
  const blocks=_assistantTurnBlocks(turn);
  if(blocks) blocks.querySelectorAll('.agent-activity-thinking').forEach(el=>el.remove());
  if(blocks) blocks.querySelectorAll('.tool-call-group[data-agent-activity-group="1"]').forEach(group=>{
    _syncToolCallGroupSummary(group);
    if(!group.querySelector('.tool-card-row,.agent-activity-thinking')){
      if(typeof _clearActivityElapsedTimer==='function') _clearActivityElapsedTimer();
      group.remove();
    }
  });
  if(turn&&blocks&&!blocks.children.length) turn.remove();
}

function fileIcon(name, type){
  if(type==='dir') return li('folder',14);
  const e=fileExt(name);
  if(IMAGE_EXTS.has(e)) return li('image',14);
  if(MD_EXTS.has(e))    return li('file-text',14);
  if(typeof DOWNLOAD_EXTS!=='undefined'&&DOWNLOAD_EXTS.has(e)) return li('download',14);
  if(e==='.py')   return li('file-code',14);
  if(e==='.js'||e==='.ts'||e==='.jsx'||e==='.tsx') return li('zap',14);
  if(e==='.json'||e==='.yaml'||e==='.yml'||e==='.toml') return li('settings',14);
  if(e==='.sh'||e==='.bash') return li('terminal',14);
  if(e==='.pdf') return li('download',14);
  return li('file-text',14);
}

function renderBreadcrumb(){
  const bar=$('breadcrumbBar');
  const upBtn=$('btnUpDir');
  if(!bar)return;
  if(S.currentDir==='.'){
    bar.style.display='none';
    if(upBtn)upBtn.style.display='none';
    return;
  }
  bar.style.display='flex';
  if(upBtn)upBtn.style.display='';
  bar.innerHTML='';
  // Root segment
  const root=document.createElement('span');
  root.className='breadcrumb-seg breadcrumb-link';
  root.textContent='~';
  root.onclick=()=>loadDir('.');
  _bindWorkspaceMoveDropTarget(root,'.');
  _bindWorkspaceOsUploadDropTarget(root,'.');
  bar.appendChild(root);
  // Path segments
  const parts=S.currentDir.split('/');
  let accumulated='';
  for(let i=0;i<parts.length;i++){
    const sep=document.createElement('span');
    sep.className='breadcrumb-sep';sep.textContent='/';
    bar.appendChild(sep);
    accumulated+=(accumulated?'/':'')+parts[i];
    const seg=document.createElement('span');
    seg.textContent=parts[i];
    if(i<parts.length-1){
      seg.className='breadcrumb-seg breadcrumb-link';
      const target=accumulated;
      seg.onclick=()=>loadDir(target);
      _bindWorkspaceMoveDropTarget(seg,target);
      _bindWorkspaceOsUploadDropTarget(seg,target);
    } else {
      seg.className='breadcrumb-seg breadcrumb-current';
    }
    bar.appendChild(seg);
  }
}

const WORKSPACE_HIDDEN_FILE_NAMES=new Set([
  '.DS_Store','._.DS_Store','.AppleDouble','.Spotlight-V100','.Trashes','.fseventsd',
  'Thumbs.db','Desktop.ini','ehthumbs.db','$RECYCLE.BIN',
  '.directory','.git','.svn','.hg','node_modules','__pycache__',
  '.pytest_cache','.mypy_cache','.ruff_cache','.tox','.venv','venv'
]);
const WORKSPACE_HIDDEN_FILE_PREFIXES=['._','.Trash-'];
function _workspaceShouldHideEntry(item){
  if(!item||S.showHiddenWorkspaceFiles)return false;
  const name=String(item.name||'');
  if(!name)return false;
  if(WORKSPACE_HIDDEN_FILE_NAMES.has(name))return true;
  return WORKSPACE_HIDDEN_FILE_PREFIXES.some(prefix=>name.startsWith(prefix));
}
function _visibleWorkspaceEntries(entries){
  const list=Array.isArray(entries)?entries:[];
  return S.showHiddenWorkspaceFiles?list:list.filter(item=>!_workspaceShouldHideEntry(item));
}
function _syncWorkspaceHiddenToggle(){
  const el=$('workspaceShowHiddenFiles');
  if(el)el.checked=!!S.showHiddenWorkspaceFiles;
  // Reflect "hidden files are visible" state on the panel heading + kebab dot,
  // so users can see they've flipped a non-default workspace pref without
  // having to open the menu. The menu itself stays out of the way otherwise.
  const ind=$('workspaceHiddenIndicator');
  if(ind){
    if(S.showHiddenWorkspaceFiles){ ind.hidden=false; ind.removeAttribute('hidden'); }
    else { ind.hidden=true; ind.setAttribute('hidden',''); }
  }
  const dot=$('workspacePrefsDot');
  if(dot){
    if(S.showHiddenWorkspaceFiles){ dot.hidden=false; dot.removeAttribute('hidden'); }
    else { dot.hidden=true; dot.setAttribute('hidden',''); }
  }
}
function toggleWorkspaceHiddenFiles(value){
  S.showHiddenWorkspaceFiles=!!value;
  try{localStorage.setItem('hermes-workspace-show-hidden-files',S.showHiddenWorkspaceFiles?'1':'0');}catch(_){}
  _syncWorkspaceHiddenToggle();
  renderFileTree();
}
try{S.showHiddenWorkspaceFiles=localStorage.getItem('hermes-workspace-show-hidden-files')==='1';}catch(_){}

// ── Workspace preferences kebab menu (#1793 UX refinement) ───────────────
// The "Show hidden files" toggle used to live as a permanent inline row
// below the breadcrumb bar. That ate ~32px of vertical space on every
// panel view (root, subdir, file preview), even though the toggle is a
// set-once preference — most users flip it once or never. Moving the
// control into a kebab dropdown reclaims the space; the small "(hidden
// files visible)" indicator on the heading reflects the non-default state
// so the affordance isn't lost.
let _workspacePrefsMenu = null;
let _workspacePrefsAnchor = null;
function _closeWorkspacePrefsMenu(){
  if(_workspacePrefsMenu){ _workspacePrefsMenu.remove(); _workspacePrefsMenu=null; }
  if(_workspacePrefsAnchor){
    _workspacePrefsAnchor.classList.remove('active');
    _workspacePrefsAnchor.setAttribute('aria-expanded','false');
    _workspacePrefsAnchor=null;
  }
}
function _positionWorkspacePrefsMenu(anchorEl){
  if(!_workspacePrefsMenu||!anchorEl) return;
  const rect=anchorEl.getBoundingClientRect();
  const menuW=Math.min(260, Math.max(220, _workspacePrefsMenu.scrollWidth||220));
  let left=rect.right-menuW;
  if(left<8) left=8;
  if(left+menuW>window.innerWidth-8) left=window.innerWidth-menuW-8;
  let top=rect.bottom+6;
  const menuH=_workspacePrefsMenu.offsetHeight||0;
  if(top+menuH>window.innerHeight-8 && rect.top>menuH+12) top=rect.top-menuH-6;
  if(top<8) top=8;
  _workspacePrefsMenu.style.left=left+'px';
  _workspacePrefsMenu.style.top=top+'px';
}
function _buildWorkspacePrefsMenu(){
  const menu=document.createElement('div');
  menu.className='workspace-prefs-menu open';
  menu.setAttribute('role','menu');
  // The checkbox keeps id="workspaceShowHiddenFiles" so existing call
  // sites (and the existing test_issue1793_file_tree_cruft_filter test)
  // can find it the same way as before. Only the parent container moves.
  const labelTxt = (typeof t==='function' ? t('workspace_show_hidden_files') : 'Show hidden files');
  const descTxt  = (typeof t==='function' ? t('workspace_show_hidden_files_desc') : 'Include .DS_Store, .git, node_modules, and other hidden / system files in the file tree.');
  const row=document.createElement('label');
  row.className='workspace-prefs-item';
  row.setAttribute('role','menuitemcheckbox');
  row.innerHTML=
    '<input type="checkbox" id="workspaceShowHiddenFiles" '+
    'onchange="toggleWorkspaceHiddenFiles(this.checked)">'+
    '<span class="workspace-prefs-copy">'+
      '<span class="workspace-prefs-name">'+esc(labelTxt)+'</span>'+
      '<span class="workspace-prefs-meta">'+esc(descTxt)+'</span>'+
    '</span>';
  const cb=row.querySelector('input');
  if(cb) cb.checked=!!S.showHiddenWorkspaceFiles;
  menu.appendChild(row);
  return menu;
}
function toggleWorkspacePrefsMenu(e){
  if(e&&e.preventDefault) e.preventDefault();
  if(e&&e.stopPropagation) e.stopPropagation();
  // Anchor preference: the kebab button. The indicator chip can also open
  // the same menu (click on "(hidden visible)"), but anchor positioning
  // always references the kebab so the menu lands in the same place.
  const anchor=$('btnWorkspacePrefs')||(e&&e.currentTarget)||null;
  if(_workspacePrefsMenu&&_workspacePrefsAnchor===anchor){ _closeWorkspacePrefsMenu(); return; }
  _closeWorkspacePrefsMenu();
  const menu=_buildWorkspacePrefsMenu();
  document.body.appendChild(menu);
  _workspacePrefsMenu=menu;
  _workspacePrefsAnchor=anchor;
  if(anchor){ anchor.classList.add('active'); anchor.setAttribute('aria-expanded','true'); }
  _positionWorkspacePrefsMenu(anchor);
}
document.addEventListener('click',e=>{
  if(!_workspacePrefsMenu) return;
  if(_workspacePrefsMenu.contains(e.target)) return;
  if(_workspacePrefsAnchor&&_workspacePrefsAnchor.contains(e.target)) return;
  // Indicator chip is also an opener — clicking it should toggle, not close.
  const ind=$('workspaceHiddenIndicator');
  if(ind&&ind.contains(e.target)) return;
  _closeWorkspacePrefsMenu();
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&_workspacePrefsMenu) _closeWorkspacePrefsMenu();
});
window.addEventListener('resize',()=>{
  if(_workspacePrefsMenu&&_workspacePrefsAnchor) _positionWorkspacePrefsMenu(_workspacePrefsAnchor);
});

if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',_syncWorkspaceHiddenToggle);
else _syncWorkspaceHiddenToggle();

function bindWorkspaceHeadingActions(){
  const heading=$('workspacePanelHeading');
  if(!heading||heading.dataset.bound==='1')return;
  heading.dataset.bound='1';
  const goRoot=()=>{
    if(S.session&&S.session.workspace) loadDir('.');
  };
  heading.onclick=goRoot;
  heading.onkeydown=(e)=>{
    if(!(S.session&&S.session.workspace)) return;
    if(e.key==='Enter'||e.key===' '){
      e.preventDefault();
      goRoot();
    }
  };
  heading.oncontextmenu=(e)=>{
    if(!(S.session&&S.session.workspace)) return;
    e.preventDefault();
    e.stopPropagation();
    _showWorkspaceRootContextMenu(e);
  };
  _syncWorkspaceHeadingState();
}

function _syncWorkspaceHeadingState(){
  const heading=$('workspacePanelHeading');
  if(!heading) return;
  const enabled=!!(S.session&&S.session.workspace);
  heading.classList.toggle('workspace-panel-heading--enabled',enabled);
  if(enabled){
    heading.setAttribute('role','button');
    heading.setAttribute('tabindex','0');
    heading.setAttribute('aria-disabled','false');
    heading.title='Workspace root';
  } else {
    heading.removeAttribute('role');
    heading.removeAttribute('tabindex');
    heading.setAttribute('aria-disabled','true');
    heading.title=t('no_workspace');
  }
}
if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',bindWorkspaceHeadingActions);
else bindWorkspaceHeadingActions();

function _workspaceContextMenuItem(label, onClick, opts={}){
  const item=document.createElement('div');
  item.textContent=label;
  item.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:'+(opts.danger?'var(--error,#e94560)':'var(--text)')+';';
  item.onmouseenter=()=>item.style.background='var(--hover-bg)';
  item.onmouseleave=()=>item.style.background='';
  item.onclick=onClick;
  return item;
}

function _copyTextWithFallback(text, successMsg, failurePrefix){
  const done=()=>showToast(successMsg);
  const fail=(err)=>showToast(failurePrefix+(err&&err.message?err.message:String(err||'')));
  if(navigator.clipboard&&navigator.clipboard.writeText){
    return navigator.clipboard.writeText(text).then(done).catch(err=>{
      const ta=document.createElement('textarea');
      ta.value=text;
      ta.style.cssText='position:fixed;left:-9999px;top:-9999px;';
      document.body.appendChild(ta);
      ta.select();
      let copied=false;
      try{copied=document.execCommand('copy');}catch(_){}
      ta.remove();
      if(copied) done(); else fail(err);
    });
  }
  const ta=document.createElement('textarea');
  ta.value=text;
  ta.style.cssText='position:fixed;left:-9999px;top:-9999px;';
  document.body.appendChild(ta);
  ta.select();
  let copied=false;
  try{copied=document.execCommand('copy');}catch(err){ta.remove();fail(err);return Promise.resolve();}
  ta.remove();
  if(copied) done(); else fail('clipboard unavailable');
  return Promise.resolve();
}

function _showWorkspaceRootContextMenu(e){
  document.querySelectorAll('.file-ctx-menu').forEach(el=>el.remove());
  const menu=document.createElement('div');
  menu.className='file-ctx-menu workspace-root-ctx-menu';
  menu.style.cssText='position:fixed;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 0;z-index:9999;min-width:160px;box-shadow:0 4px 16px rgba(0,0,0,.35);';
  const vw=window.innerWidth,vh=window.innerHeight;
  menu.style.left=(e.clientX+160>vw?e.clientX-170:e.clientX)+'px';
  menu.style.top=(e.clientY+80>vh?e.clientY-80:e.clientY)+'px';

  menu.appendChild(_workspaceContextMenuItem(t('reveal_in_finder'),async()=>{
    menu.remove();
    try{await api('/api/file/reveal',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:'.'})});}
    catch(err){showToast(t('reveal_failed')+(err.message||err));}
  }));

  menu.appendChild(_workspaceContextMenuItem(t('open_in_vscode'),async()=>{
    menu.remove();
    try{await api('/api/file/open-vscode',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:'.'})});}
    catch(err){showToast(t('open_in_vscode_failed')+(err.message||err));}
  }));

  menu.appendChild(_workspaceContextMenuItem(t('copy_file_path'),async()=>{
    menu.remove();
    try{
      const r=await api('/api/file/path',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:'.'})});
      await _copyTextWithFallback((r&&r.path)||'.',t('path_copied'),t('path_copy_failed'));
    }catch(err){showToast(t('path_copy_failed')+(err.message||err));}
  }));

  document.body.appendChild(menu);
  const dismiss=()=>{menu.remove();document.removeEventListener('click',dismiss);};
  setTimeout(()=>document.addEventListener('click',dismiss),0);
}

// Track expanded directories for tree view
if(!S._expandedDirs) S._expandedDirs=new Set();
// Cache of fetched directory contents: path -> entries[]
if(!S._dirCache) S._dirCache={};

function renderFileTree(){
  const box=$('fileTree');box.innerHTML='';
  // Cache current dir entries
  S._dirCache[S.currentDir||'.']=S.entries;
  // Show empty-state when no workspace is set or the directory is empty (#703)
  const emptyEl=$('wsEmptyState');
  const hasWorkspace=!!(S.session&&S.session.workspace);
  if(!hasWorkspace){
    if(emptyEl){emptyEl.textContent=t('workspace_empty_no_path');emptyEl.style.display='flex';}
    box.style.display='none';
    return;
  }
  if(emptyEl) emptyEl.style.display='none';
  box.style.display='';
  const visibleEntries=_visibleWorkspaceEntries(S.entries);
  if(!visibleEntries.length){
    if(emptyEl){emptyEl.textContent=t('workspace_empty_dir');emptyEl.style.display='flex';}
    return;
  }
  _renderTreeItems(box, visibleEntries, 0);
}

function _isWorkspaceTreeMoveDrag(e){
  return !!(e.dataTransfer&&e.dataTransfer.types&&e.dataTransfer.types.includes('application/ws-path')&&!e.dataTransfer.types.includes('Files'));
}

function _workspaceParentDir(relPath){
  if(!relPath||relPath==='.')return '.';
  const idx=relPath.lastIndexOf('/');
  return idx===-1?'.':relPath.substring(0,idx);
}

function _clearWorkspaceMoveDragOver(){
  document.querySelectorAll('.file-item.drag-over,.breadcrumb-seg.drag-over').forEach(el=>el.classList.remove('drag-over'));
}

function _remapWorkspaceCachesAfterMove(oldPath,newPath,isDir){
  if(isDir&&S._expandedDirs){
    if(S._expandedDirs.has(oldPath)){
      S._expandedDirs.delete(oldPath);
      S._expandedDirs.add(newPath);
    }
    for(const expandedPath of [...S._expandedDirs]){
      if(expandedPath.startsWith(oldPath+'/')){
        S._expandedDirs.delete(expandedPath);
        S._expandedDirs.add(newPath+expandedPath.slice(oldPath.length));
      }
    }
    if(S._dirCache[oldPath]){
      S._dirCache[newPath]=S._dirCache[oldPath];
      delete S._dirCache[oldPath];
    }
    for(const cachePath of Object.keys(S._dirCache)){
      if(cachePath.startsWith(oldPath+'/')){
        const remapped=newPath+cachePath.slice(oldPath.length);
        S._dirCache[remapped]=S._dirCache[cachePath];
        delete S._dirCache[cachePath];
      }
    }
    if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
  }
  delete S._dirCache[_workspaceParentDir(oldPath)];
  delete S._dirCache[_workspaceParentDir(newPath)];
  if(typeof _previewCurrentPath!=='undefined'&&_previewCurrentPath){
    if(_previewCurrentPath===oldPath)_previewCurrentPath=newPath;
    else if(_previewCurrentPath.startsWith(oldPath+'/'))_previewCurrentPath=newPath+_previewCurrentPath.slice(oldPath.length);
  }
}

async function _performWorkspaceMove(srcPath,destDir,isDir){
  if(!S.session||!srcPath)return;
  const normDest=destDir||'.';
  if(srcPath===normDest)return;
  if(normDest.startsWith(srcPath+'/'))return;
  if(_workspaceParentDir(srcPath)===normDest)return;
  try{
    const data=await api('/api/file/move',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id,path:srcPath,dest_dir:normDest
    })});
    const movedName=data.new_path.includes('/')?data.new_path.slice(data.new_path.lastIndexOf('/')+1):data.new_path;
    showToast((t('moved_to')||'Moved to ')+movedName);
    _remapWorkspaceCachesAfterMove(data.old_path||srcPath,data.new_path||srcPath,isDir);
    await loadDir(S.currentDir);
    if(typeof refreshOpenPreviewIfMutated==='function')await refreshOpenPreviewIfMutated();
  }catch(err){
    showToast((t('move_failed')||'Move failed: ')+err.message,5000,'error');
  }
}

function _bindWorkspaceMoveDropTarget(el,destDir){
  el.ondragenter=(e)=>{
    if(!_isWorkspaceTreeMoveDrag(e))return;
    e.preventDefault();e.stopPropagation();
    el.classList.add('drag-over');
  };
  el.ondragover=(e)=>{
    if(!_isWorkspaceTreeMoveDrag(e))return;
    e.preventDefault();e.stopPropagation();
    e.dataTransfer.dropEffect='move';
    el.classList.add('drag-over');
  };
  el.ondragleave=(e)=>{
    if(el.contains(e.relatedTarget))return;
    el.classList.remove('drag-over');
  };
  el.ondrop=async(e)=>{
    if(!_isWorkspaceTreeMoveDrag(e))return;
    e.preventDefault();e.stopPropagation();
    el.classList.remove('drag-over');
    const srcPath=e.dataTransfer.getData('application/ws-path');
    if(!srcPath)return;
    const srcType=e.dataTransfer.getData('application/ws-type');
    await _performWorkspaceMove(srcPath,destDir,srcType==='dir');
  };
}

function _renderTreeItems(container, entries, depth){
  for(const item of entries){
    const el=document.createElement('div');el.className='file-item';
    el.style.paddingLeft=(8+depth*16)+'px';
    el.setAttribute('draggable','true');
    el.dataset.wsType=item.type;
    el.oncontextmenu=(e)=>{e.preventDefault();e.stopPropagation();_showFileContextMenu(e,item);};
    el.ondragstart=(e)=>{e.dataTransfer.setData('application/ws-path',item.path);e.dataTransfer.setData('application/ws-type',item.type);e.dataTransfer.effectAllowed='copy';el.classList.add('dragging');};
    el.ondragend=()=>{el.classList.remove('dragging');_clearWorkspaceMoveDragOver();};

    if(item.type==='dir'){
      // Toggle arrow for directories
      const arrow=document.createElement('span');
      arrow.className='file-tree-toggle';
      const isExpanded=S._expandedDirs.has(item.path);
      arrow.textContent=isExpanded?'\u25BE':'\u25B8';
      el.appendChild(arrow);
    }else{
      // Keep file icons aligned with sibling directories that occupy this
      // slot with the expand/collapse toggle. #2554
      const spacer=document.createElement('span');
      spacer.className='file-tree-toggle-placeholder';
      spacer.setAttribute('aria-hidden','true');
      el.appendChild(spacer);
    }

    // Icon
    const iconEl=document.createElement('span');
    iconEl.className='file-icon';iconEl.innerHTML=fileIcon(item.name,item.type);
    el.appendChild(iconEl);

    // Name
    const nameEl=document.createElement('span');
    nameEl.className='file-name';nameEl.textContent=item.name;
    // Tooltip only on FILES — dblclick renames them. On directories, dblclick
    // navigates into the folder; rename lives in the right-click context menu
    // (the "Double-click to rename" hint here would be misleading). #1710.
    if(item.type!=='dir')nameEl.title=t('double_click_rename');
    // Single-click opens (file) or expand-toggles (dir) but is debounced 300ms so a
    // double-click can cancel it and trigger rename instead. Without the debounce, the
    // click bubbles to el.onclick before dblclick can fire — that's #1698. Without the
    // restored activation, single-click on the filename does nothing — that's #1707.
    let _nameClickTimer=null;
    nameEl.onclick=(e)=>{
      e.stopPropagation();
      if(_nameClickTimer){clearTimeout(_nameClickTimer);_nameClickTimer=null;}
      _nameClickTimer=setTimeout(()=>{
        _nameClickTimer=null;
        // Delegate to the row's existing single-click handler (openFile / dir toggle).
        if(typeof el.onclick==='function')el.onclick(e);
      },300);
    };
    nameEl.ondblclick=(e)=>{
      e.stopPropagation();
      if(_nameClickTimer){clearTimeout(_nameClickTimer);_nameClickTimer=null;}
      // For directories, double-click navigates (breadcrumb view)
      if(item.type==='dir'){loadDir(item.path);return;}
      const inp=document.createElement('input');
      inp.className='file-rename-input';inp.value=item.name;
      inp.onclick=(e2)=>e2.stopPropagation();
      const finish=async(save)=>{
        inp.onblur=null;
        if(save){
          const newName=inp.value.trim();
          if(newName&&newName!==item.name){
            try{
              await api('/api/file/rename',{method:'POST',body:JSON.stringify({
                session_id:S.session.session_id,path:item.path,new_name:newName
              })});
              showToast(t('renamed_to')+newName);
              // Update expanded dirs cache key if renaming a directory
              if(item.type==='dir'&&S._expandedDirs){
                S._expandedDirs.delete(item.path);
                const parent=item.path.includes('/')?item.path.substring(0,item.path.lastIndexOf('/')):'.';
                const newPath=parent==='.'?newName:parent+'/'+newName;
                S._expandedDirs.add(newPath);
                if(S._dirCache[item.path]){S._dirCache[newPath]=S._dirCache[item.path];delete S._dirCache[item.path];}
                if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
              }
              // Invalidate cache and re-render
              delete S._dirCache[S.currentDir];
              await loadDir(S.currentDir);
            }catch(err){showToast(t('rename_failed')+err.message);}
          }
        }
        inp.replaceWith(nameEl);
      };
      inp.onkeydown=(e2)=>{
        if(e2.key==='Enter'){
          if(window._isImeEnter&&window._isImeEnter(e2)){return;}
          e2.preventDefault();
          finish(true);
        }
        if(e2.key==='Escape'){e2.preventDefault();finish(false);}
      };
      inp.onblur=()=>finish(false);
      nameEl.replaceWith(inp);
      setTimeout(()=>{inp.focus();inp.select();},10);
    };
    el.appendChild(nameEl);

    // Size -- only for files
    if(item.type==='file'&&item.size){
      const sizeEl=document.createElement('span');
      sizeEl.className='file-size';
      sizeEl.textContent=`${(item.size/1024).toFixed(1)}k`;
      el.appendChild(sizeEl);
    }

    // Delete button -- for files and directories
    if(item.type==='file'){
      const del=document.createElement('button');
      del.className='file-del-btn';del.title=t('delete_title');del.textContent='\u00d7';
      del.onclick=async(e)=>{e.stopPropagation();await deleteWorkspaceFile(item.path,item.name);};
      el.appendChild(del);
    }else if(item.type==='dir'){
      const del=document.createElement('button');
      del.className='file-del-btn';del.title=t('delete_title');del.textContent='\u00d7';
      del.onclick=async(e)=>{e.stopPropagation();await deleteWorkspaceDir(item.path,item.name);};
      el.appendChild(del);
    }

    if(item.type==='dir'){
      _bindWorkspaceMoveDropTarget(el,item.path);
      _bindWorkspaceOsUploadDropTarget(el,item.path);
      // Single-click toggles expand/collapse
      el.onclick=async(e)=>{
        e.stopPropagation();
        if(S._expandedDirs.has(item.path)){
          S._expandedDirs.delete(item.path);
          if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
          renderFileTree();
        }else{
          S._expandedDirs.add(item.path);
          if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
          // Fetch children if not cached
          if(!S._dirCache[item.path]){
            try{
              const data=await api(`/api/list?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(item.path)}`);
              S._dirCache[item.path]=data.entries||[];
            }catch(e2){S._dirCache[item.path]=[];}
          }
          renderFileTree();
        }
      };
    }else{
      el.onclick=async()=>openFile(item.path);
    }

    container.appendChild(el);

    // Render children if directory is expanded
    if(item.type==='dir'&&S._expandedDirs.has(item.path)){
      const children=_visibleWorkspaceEntries(S._dirCache[item.path]||[]);
      if(children.length){
        _renderTreeItems(container, children, depth+1);
      }else{
        const empty=document.createElement('div');
        empty.className='file-item file-empty';
        empty.style.paddingLeft=(8+(depth+1)*16)+'px';
        empty.textContent=t('empty_dir');
        container.appendChild(empty);
      }
    }
  }
}

async function deleteWorkspaceDir(relPath, name){
  if(!S.session)return;
  const ok=await showConfirmDialog({title:t('delete_dir_confirm',name),message:'',confirmLabel:'Delete',danger:true,focusCancel:true});
  if(!ok)return;
  try{
    await api('/api/file/delete',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath,recursive:true})});
    showToast(t('deleted')+name);
    // Remove from expanded dirs cache
    if(S._expandedDirs){S._expandedDirs.delete(relPath);if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();}
    delete S._dirCache[relPath];
    await loadDir(S.currentDir);
  }catch(e){setStatus(t('delete_failed')+e.message);}
}

function _showFileContextMenu(e, item){
  document.querySelectorAll('.file-ctx-menu').forEach(el=>el.remove());
  const menu=document.createElement('div');
  menu.className='file-ctx-menu';
  menu.style.cssText='position:fixed;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 0;z-index:9999;min-width:140px;box-shadow:0 4px 16px rgba(0,0,0,.35);';
  // Keep menu within viewport
  const vw=window.innerWidth,vh=window.innerHeight;
  menu.style.left=(e.clientX+140>vw?e.clientX-150:e.clientX)+'px';
  menu.style.top=(e.clientY+100>vh?e.clientY-100:e.clientY)+'px';

  // Rename
  const renameItem=document.createElement('div');
  renameItem.textContent=t('rename_title');
  renameItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
  renameItem.onmouseenter=()=>renameItem.style.background='var(--hover-bg)';
  renameItem.onmouseleave=()=>renameItem.style.background='';
  renameItem.onclick=()=>{menu.remove();_inlineRenameFileItem(item);};
  menu.appendChild(renameItem);

  // Reveal in File Manager
  const revealItem=document.createElement('div');
  revealItem.textContent=t('reveal_in_finder');
  revealItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
  revealItem.onmouseenter=()=>revealItem.style.background='var(--hover-bg)';
  revealItem.onmouseleave=()=>revealItem.style.background='';
  revealItem.onclick=async()=>{menu.remove();try{await api('/api/file/reveal',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:item.path})});}catch(err){showToast(t('reveal_failed')+(err.message||err));}};
  menu.appendChild(revealItem);

  // Open in VS Code (#2735)
  const vscodeItem=document.createElement('div');
  vscodeItem.textContent=t('open_in_vscode');
  vscodeItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
  vscodeItem.onmouseenter=()=>vscodeItem.style.background='var(--hover-bg)';
  vscodeItem.onmouseleave=()=>vscodeItem.style.background='';
  vscodeItem.onclick=async()=>{menu.remove();try{await api('/api/file/open-vscode',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:item.path})});}catch(err){showToast(t('open_in_vscode_failed')+(err.message||err));}};
  menu.appendChild(vscodeItem);

  // Copy file path — resolves the absolute on-disk path on the server (so the
  // user gets the full /home/.../workspace/foo.py rather than the relative
  // path the file tree shows) and writes it to the OS clipboard. Useful for
  // pasting into terminals, editors, or other apps without taking the slower
  // Reveal-in-Finder round trip.
  const copyPathItem=document.createElement('div');
  copyPathItem.textContent=t('copy_file_path');
  copyPathItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
  copyPathItem.onmouseenter=()=>copyPathItem.style.background='var(--hover-bg)';
  copyPathItem.onmouseleave=()=>copyPathItem.style.background='';
  copyPathItem.onclick=async()=>{
    menu.remove();
    try{
      const r=await api('/api/file/path',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:item.path})});
      const abs=(r&&r.path)||item.path;
      try{
        await navigator.clipboard.writeText(abs);
        showToast(t('path_copied'));
      }catch(clipErr){
        // Fallback for browsers where Clipboard API is gated (older Safari,
        // non-secure contexts). Use the legacy execCommand path against a
        // hidden textarea — this is the same pattern boot.js uses for the
        // "Copy" buttons on code blocks.
        const ta=document.createElement('textarea');
        ta.value=abs;
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
    }
  };
  menu.appendChild(copyPathItem);

  // Download as zip — only for directories. Streams the folder contents
  // through /api/folder/download which builds the zip on the fly.
  if(item.type==='dir'){
    const dlItem=document.createElement('div');
    dlItem.textContent=t('download_folder');
    dlItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
    dlItem.onmouseenter=()=>dlItem.style.background='var(--hover-bg)';
    dlItem.onmouseleave=()=>dlItem.style.background='';
    dlItem.onclick=()=>{
      menu.remove();
      const url='/api/folder/download?session_id='+encodeURIComponent(S.session.session_id)
              + '&path='+encodeURIComponent(item.path||'');
      window.location.href=url;
    };
    menu.appendChild(dlItem);
  }

  // Divider + Delete
  const sep=document.createElement('hr');
  sep.style.cssText='border:none;border-top:1px solid var(--border);margin:4px 0;';
  menu.appendChild(sep);
  const delItem=document.createElement('div');
  delItem.textContent=t('delete_title');
  delItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--error,#e94560);';
  delItem.onmouseenter=()=>delItem.style.background='var(--hover-bg)';
  delItem.onmouseleave=()=>delItem.style.background='';
  delItem.onclick=()=>{menu.remove();if(item.type==='dir')deleteWorkspaceDir(item.path,item.name);else deleteWorkspaceFile(item.path,item.name);};
  menu.appendChild(delItem);

  document.body.appendChild(menu);
  const dismiss=()=>{menu.remove();document.removeEventListener('click',dismiss);};
  setTimeout(()=>document.addEventListener('click',dismiss),0);
}

async function _inlineRenameFileItem(item){
  if(!S.session)return;
  // Pre-fill the input with the current name and select just the stem
  // (everything before the last '.') so the user can immediately retype the
  // basename while preserving the extension — matches macOS Finder. For
  // directories or names with no '.', the helper selects the full value.
  // `selectStem` also handles dotfiles ('.gitignore') by full-selecting.
  const newName=await showPromptDialog({
    message:t('rename_prompt'),
    value:item.name,
    confirmLabel:t('rename_title'),
    selectStem:item.type!=='dir',
    selectAll:item.type==='dir'
  });
  if(!newName||newName===item.name)return;
  try{
    await api('/api/file/rename',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:item.path,new_name:newName})});
    showToast(t('renamed_to')+newName);
    // Update expanded dirs cache key if renaming a directory
    if(item.type==='dir'&&S._expandedDirs){
      S._expandedDirs.delete(item.path);
      const parent=item.path.includes('/')?item.path.substring(0,item.path.lastIndexOf('/')):'.';
      const newPath=parent==='.'?newName:parent+'/'+newName;
      S._expandedDirs.add(newPath);
      if(S._dirCache[item.path]){S._dirCache[newPath]=S._dirCache[item.path];delete S._dirCache[item.path];}
      if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
    }
    delete S._dirCache[S.currentDir];
    await loadDir(S.currentDir);
  }catch(err){showToast(t('rename_failed')+err.message);}
}

async function deleteWorkspaceFile(relPath, name){
  if(!S.session)return;
  const _delFile=await showConfirmDialog({title:t('delete_confirm',name),message:'',confirmLabel:'Delete',danger:true,focusCancel:true});
  if(!_delFile) return;
  try{
    await api('/api/file/delete',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath})});
    showToast(t('deleted')+name);
    // Close preview if we just deleted the viewed file
    if($('previewPathText').textContent===relPath)$('btnClearPreview').onclick();
    await loadDir(S.currentDir);
  }catch(e){setStatus(t('delete_failed')+e.message);}
}

async function promptNewFile(){
  // If no active session but a default workspace is configured, auto-create
  // a session bound to it so workspace actions work on the blank new-chat page.
  if(!S.session){
    const ws=(typeof S._profileDefaultWorkspace==='string'&&S._profileDefaultWorkspace)||'';
    if(!ws) return;
    try{
      const r=await api('/api/session/new',{method:'POST',body:JSON.stringify({workspace:ws})});
      if(r&&r.session){S.session=r.session;S.messages=[];syncTopbar();renderMessages();await renderSessionList();}
    }catch(e){setStatus(t('create_failed')+e.message);return;}
  }
  if(!S.session)return;
  const name=await showPromptDialog({title:t('new_file_prompt'),placeholder:'filename.txt',confirmLabel:t('create')});
  if(!name||!name.trim())return;
  const relPath=S.currentDir==='.'?name.trim():(S.currentDir+'/'+name.trim());
  try{
    await api('/api/file/create',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath,content:''})});
    showToast(t('created')+name.trim());
    await loadDir(S.currentDir);
    openFile(relPath);
  }catch(e){setStatus(t('create_failed')+e.message);}
}

async function promptNewFolder(){
  // Same auto-create-session logic as promptNewFile for the blank page.
  if(!S.session){
    const ws=(typeof S._profileDefaultWorkspace==='string'&&S._profileDefaultWorkspace)||'';
    if(!ws) return;
    try{
      const r=await api('/api/session/new',{method:'POST',body:JSON.stringify({workspace:ws})});
      if(r&&r.session){S.session=r.session;S.messages=[];syncTopbar();renderMessages();await renderSessionList();}
    }catch(e){setStatus(t('folder_create_failed')+e.message);return;}
  }
  if(!S.session)return;
  const name=await showPromptDialog({title:t('new_folder_prompt'),placeholder:'folder-name',confirmLabel:t('create')});
  if(!name||!name.trim())return;
  const relPath=S.currentDir==='.'?name.trim():(S.currentDir+'/'+name.trim());
  try{
    await api('/api/file/create-dir',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath})});
    showToast(t('folder_created')+name.trim());
    await loadDir(S.currentDir);
    // Offer to add the new folder as a space (#782)
    const absPath=S.session.workspace?((S.currentDir==='.'?S.session.workspace:S.session.workspace+'/'+S.currentDir)+'/'+name.trim()):null;
    if(absPath){
      const addAsSpace=await showConfirmDialog({
        title:t('folder_add_as_space_title'),
        message:t('folder_add_as_space_msg'),
        confirmLabel:t('folder_add_as_space_btn'),
        focusCancel:true
      });
      if(addAsSpace){
        try{
          const data=await api('/api/workspaces/add',{method:'POST',body:JSON.stringify({path:absPath})});
          if(typeof _workspaceList!=='undefined')_workspaceList=data.workspaces||_workspaceList||[];
          if(typeof renderWorkspacesPanel==='function')renderWorkspacesPanel(_workspaceList);
          showToast(t('workspace_added'));
        }catch(e2){setStatus((t('error_prefix')||'Error: ')+e2.message);}
      }
    }
  }catch(e){setStatus(t('folder_create_failed')+e.message);}
}

function renderTray(){ // non-media files use paperclip chip
  const tray=$('attachTray');tray.innerHTML='';
  if(!S.pendingFiles.length){tray.classList.remove('has-files');updateSendBtn();return;}
  tray.classList.add('has-files');
  updateSendBtn();
  S.pendingFiles.forEach((f,i)=>{
    const chip=document.createElement('div');chip.className='attach-chip';
    const mediaKind=_mediaKindForName(f.name);
    if(_IMAGE_EXTS.test(f.name)||mediaKind==='audio'||mediaKind==='video'){
      const blobUrl=URL.createObjectURL(f);
      chip.className='attach-chip attach-chip--media attach-chip--'+mediaKind; // attach-chip--audio attach-chip--video
      chip.dataset.blobUrl=blobUrl;
      if(mediaKind==='image'){
        chip.innerHTML=`<img class="attach-thumb" src="${esc(blobUrl)}" alt="${esc(f.name)}" title="${esc(f.name)}"><button title="${t('remove_title')}">${li('x',12)}</button>`;
      } else if(_SVG_EXTS.test(f.name)){
        chip.innerHTML=`<img class="attach-thumb attach-thumb--svg" src="${esc(blobUrl)}" alt="${esc(f.name)}" title="${esc(f.name)}"><button title="${t('remove_title')}">${li('x',12)}</button>`;
      } else if(mediaKind==='audio'){
        chip.innerHTML=`<span class="attach-chip-media">🎵 ${esc(f.name)}</span><audio controls preload="metadata" src="${esc(blobUrl)}"></audio><button title="${t('remove_title')}">${li('x',12)}</button>`;
      } else if(mediaKind==='video'){
        chip.innerHTML=`<span class="attach-chip-media">🎬 ${esc(f.name)}</span><video controls preload="metadata" src="${esc(blobUrl)}"></video><button title="${t('remove_title')}">${li('x',12)}</button>`;
      }
    } else {
      chip.innerHTML=`${li('paperclip',12)} ${esc(f.name)} <button title="${t('remove_title')}">${li('x',12)}</button>`;
    }
    chip.querySelector('button').onclick=()=>{
      // Revoke blob URL to avoid memory leak before removing
      if(chip.dataset.blobUrl) URL.revokeObjectURL(chip.dataset.blobUrl);
      S.pendingFiles.splice(i,1);renderTray();
    };
    tray.appendChild(chip);
  });
}
function _uploadTooLargeMessage(file){
  const fileSizeMb=Math.ceil(((file&&file.size)||0)/1024/1024);
  return t('upload_too_large',MAX_UPLOAD_MB,fileSizeMb);
}
function _showUploadTooLarge(file){
  const message=`${t('upload_failed')}${file&&file.name?file.name:'file'} \u2014 ${_uploadTooLargeMessage(file)}`;
  if(typeof setStatus==='function')setStatus(`\u274c ${message}`);
  else if(typeof showToast==='function')showToast(message,5000,'error');
}
function addFiles(files){
  for(const f of files){
    if(f&&f.size>MAX_UPLOAD_BYTES){_showUploadTooLarge(f);continue;}
    if(!S.pendingFiles.find(p=>p.name===f.name))S.pendingFiles.push(f);
  }
  renderTray();
}
async function uploadPendingFiles(){
  if(!S.pendingFiles.length||!S.session)return[];
  const names=[];let failures=0;
  const bar=$('uploadBar');const barWrap=$('uploadBarWrap');
  barWrap.classList.add('active');bar.style.width='0%';
  const total=S.pendingFiles.length;
  for(let i=0;i<total;i++){
    const f=S.pendingFiles[i];
    try{
      if(f&&f.size>MAX_UPLOAD_BYTES)throw new Error(_uploadTooLargeMessage(f));
      const fd=new FormData();
      fd.append('session_id',S.session.session_id);fd.append('file',f,f.name);
      const isArchive=_ARCHIVE_EXTS.test(f.name);
      const url=new URL(isArchive?'api/upload/extract':'api/upload',document.baseURI||location.href).href;
      const res=await fetch(url,{method:'POST',credentials:'include',body:fd});
      if(_redirectIfUnauth(res)) return;
      if(!res.ok){const err=await res.text();throw new Error(err);}
      const data=await res.json();
      if(data.error)throw new Error(data.error);
      if(isArchive){
        names.push({name: data.dest, path: data.dest, extracted: data.extracted});
        if(typeof loadDir==='function')loadDir(S.currentDir||'.');
      }else{
        names.push({name: data.filename, path: data.path, mime: data.mime, size: data.size, is_image: !!data.is_image});
      }
    }catch(e){failures++;setStatus(`\u274c ${t('upload_failed')}${f.name} \u2014 ${e.message}`);}
    bar.style.width=`${Math.round((i+1)/total*100)}%`;
  }
  barWrap.classList.remove('active');bar.style.width='0%';
  S.pendingFiles=[];renderTray();
  if(failures===total&&total>0)throw new Error(t('all_uploads_failed',total));
  // Show extraction summary
  const extracted=names.filter(n=>n.extracted);
  if(extracted.length)showToast(t('archive_extracted',extracted.reduce((s,n)=>s+n.extracted,0),extracted.length));
  return names;
}
