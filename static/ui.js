const S={session:null,messages:[],entries:[],busy:false,pendingFiles:[],toolCalls:[],activeStreamId:null};
const INFLIGHT={};  // keyed by session_id while request in-flight
const MSG_QUEUE=[];  // messages queued while a request is in-flight
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// Relative time helper -- "just now", "5m ago", "3h ago", "2 days ago", etc.
function _relTime(tsMs){
  const diff=Date.now()-tsMs;
  if(diff<60000) return 'just now';
  if(diff<3600000) return Math.floor(diff/60000)+'m ago';
  if(diff<86400000) return Math.floor(diff/3600000)+'h ago';
  if(diff<86400000*2) return 'yesterday';
  if(diff<86400000*30) return Math.floor(diff/86400000)+' days ago';
  if(diff<86400000*365) return Math.floor(diff/86400000/30)+'mo ago';
  return Math.floor(diff/86400000/365)+'y ago';
}

function _fmtTokens(n){
  if(n>=1000000) return (n/1000000).toFixed(1)+'M';
  if(n>=1000) return (n/1000).toFixed(1)+'k';
  return String(n);
}

// ── JSON syntax highlighter ──
function highlightJSON(str){
  // tokenise the already-escaped string coming back from JSON.stringify
  // We re-escape it ourselves so we control the markup.
  let raw;
  try{ raw=JSON.stringify(JSON.parse(str),null,2); }
  catch(e){ return `<span class="jh-str">${esc(str)}</span>`; }
  return esc(raw).replace(
    /(&quot;)((?:[^&]|&(?!quot;))*?)(&quot;)(\s*:)?|(\btrue\b|\bfalse\b|\bnull\b)|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (m,oq,key,cq,colon,kw,num)=>{
      if(oq&&colon) return `<span class="jh-key">${oq}${key}${cq}</span>${colon}`;
      if(oq)        return `<span class="jh-str">${oq}${key}${cq}</span>`;
      if(kw)        return `<span class="jh-kw">${kw}</span>`;
      if(num!==undefined) return `<span class="jh-num">${num}</span>`;
      return m;
    }
  );
}

// Try to pretty-print a value for display in a tool arg or result.
// Returns HTML string.
function fmtArgVal(v){
  if(v===null||v===undefined) return `<span class="jh-kw">null</span>`;
  if(typeof v==='boolean')    return `<span class="jh-kw">${v}</span>`;
  if(typeof v==='number')     return `<span class="jh-num">${esc(String(v))}</span>`;
  if(typeof v==='object')     return highlightJSON(JSON.stringify(v));
  // string -- try to detect embedded JSON
  const trimmed=v.trim();
  if((trimmed.startsWith('{')||trimmed.startsWith('['))&&trimmed.length<8000){
    try{ JSON.parse(trimmed); return highlightJSON(trimmed); }catch(e){}
  }
  return `<span class="jh-str">${esc(v)}</span>`;
}

// Try to extract a clean display string from a tool result JSON envelope.
// Tools like terminal return {"output": "...", "exit_code": 0, ...}.
// Returns {text, isTerminal} if detected, null otherwise.
function _extractToolOutput(raw, toolName){
  if(!raw) return null;
  const t=raw.trim();
  if(!t.startsWith('{')) return null;
  try{
    const obj=JSON.parse(t);
    // Detect success:false on any JSON result -- treated like a soft error
    const isSoftError=(obj.success===false);
    // Terminal / execute_code results: show output as terminal text
    if(typeof obj.output==='string' && (toolName==='terminal'||toolName==='execute_code')){
      const exitCode=obj.exit_code!=null?obj.exit_code:'';
      const errStr=(obj.error&&typeof obj.error==='string')?'\n'+obj.error:'';
      return {text:obj.output+errStr, exitCode, isTerminal:true, isSoftError};
    }
    // read_file results: show content directly
    if(typeof obj.content==='string' && toolName==='read_file'){
      return {text:obj.content, isTerminal:false, isSoftError};
    }
    // search_files: show matches as plain text
    if(Array.isArray(obj.matches) && (toolName==='search_files')){
      const lines=obj.matches.map(m=>{
        if(m.line!=null) return `${m.path||''}:${m.line}: ${m.content||''}`;
        return m.path||m.file||JSON.stringify(m);
      });
      return {text:lines.join('\n'), isTerminal:false, isSoftError};
    }
    // web_search: show results cleanly
    if(obj.data&&Array.isArray(obj.data.web)&&toolName==='web_search'){
      const lines=obj.data.web.map(r=>`${r.title||''}\n  ${r.url||''}\n  ${r.description||''}`);
      return {text:lines.join('\n\n'), isTerminal:false, isSoftError};
    }
    // browser_navigate result
    if(toolName==='browser_navigate'){
      const title=obj.title||obj.page_title||'';
      const url=obj.url||'';
      const text=(title&&url)?`${title}\n${url}`:title||url||JSON.stringify(obj,null,2);
      return {text, isTerminal:false, isSoftError};
    }
    // web_extract: show title + first chunk of content
    if(toolName==='web_extract'&&Array.isArray(obj.results)){
      const lines=obj.results.map(r=>`${r.title||r.url||''}${r.content?'\n'+r.content.slice(0,300):''}`);
      return {text:lines.join('\n\n---\n'), isTerminal:false, isSoftError};
    }
    // mcp_todo result: render todo list as structured HTML
    if(toolName==='todo'&&Array.isArray(obj.todos)){
      const sum=obj.summary;
      const summary=sum?`${sum.total||obj.todos.length} tasks · ${sum.completed||0} done · ${sum.in_progress||0} active · ${sum.pending||0} pending`:'';
      return {text:summary, isTodoHtml:true, todos:obj.todos, isSoftError};
    }
    // patch result: render as diff (handled specially in buildToolCard)
    if(toolName==='patch'&&typeof obj.diff==='string'){
      return {text:obj.diff, isTerminal:false, isSoftError, isDiff:true,
              files:Array.isArray(obj.files_modified)?obj.files_modified:[],
              lint:obj.lint||null};
    }
    // Any other JSON with success:false -- show error message or raw
    if(isSoftError){
      const msg=obj.error||obj.message||obj.detail||JSON.stringify(obj,null,2);
      return {text:String(msg), isTerminal:false, isSoftError:true};
    }
  }catch(e){}
  return null;
}

// Format the snippet (tool result). Detect JSON, pretty-print, highlight.
function fmtSnippet(raw){
  if(!raw) return '';
  const t=raw.trim();
  if((t.startsWith('{')||t.startsWith('['))&&t.length<16000){
    try{ return highlightJSON(t); }catch(e){}
  }
  return esc(raw);
}

// Dynamic model labels -- populated by populateModelDropdown(), fallback to static map
let _dynamicModelLabels={};

async function populateModelDropdown(){
  const sel=$('modelSelect');
  if(!sel) return;
  try{
    const data=await fetch('/api/models',{credentials:'include'}).then(r=>r.json());
    if(!data.groups||!data.groups.length) return; // keep HTML defaults
    // Clear existing options
    sel.innerHTML='';
    _dynamicModelLabels={};
    for(const g of data.groups){
      const og=document.createElement('optgroup');
      og.label=g.provider;
      for(const m of g.models){
        const opt=document.createElement('option');
        opt.value=m.id;
        opt.textContent=m.label;
        og.appendChild(opt);
        _dynamicModelLabels[m.id]=m.label;
      }
      sel.appendChild(og);
    }
    // Set default model from server if no localStorage preference
    if(data.default_model && !localStorage.getItem('hermes-webui-model')){
      sel.value=data.default_model;
      // If the default isn't in the list, add it
      if(sel.value!==data.default_model){
        const opt=document.createElement('option');
        opt.value=data.default_model;
        opt.textContent=data.default_model.split('/').pop();
        sel.insertBefore(opt,sel.firstChild);
        sel.value=data.default_model;
      }
    }
  }catch(e){
    // API unavailable -- keep the hardcoded HTML options as fallback
    console.warn('Failed to load models from server:',e.message);
  }
  // Rebuild the custom dropdown to reflect any server-populated options
  if(typeof buildModelCSelect==='function') buildModelCSelect();
}

// ── Scroll pinning ──────────────────────────────────────────────────────────
// When streaming, auto-scroll only if the user hasn't manually scrolled up.
// Once the user scrolls back to within 80px of the bottom, re-pin.
let _scrollPinned=true;
(function(){
  const el=document.getElementById('messages');
  if(!el) return;
  el.addEventListener('scroll',()=>{
    const nearBottom=el.scrollHeight-el.scrollTop-el.clientHeight<80;
    _scrollPinned=nearBottom;
  });
})();
function scrollIfPinned(){
  if(!_scrollPinned) return;
  const el=$('messages');
  if(el) el.scrollTop=el.scrollHeight;
}
function scrollToBottom(){
  _scrollPinned=true;
  const el=$('messages');
  if(el) el.scrollTop=el.scrollHeight;
}

function getModelLabel(modelId){
  if(!modelId) return 'Unknown';
  // Check dynamic labels first, then fall back to splitting the ID
  if(_dynamicModelLabels[modelId]) return _dynamicModelLabels[modelId];
  // Static fallback for common models
  const STATIC_LABELS={'openai/gpt-5.4-mini':'GPT-5.4 Mini','openai/gpt-4o':'GPT-4o','openai/o3':'o3','openai/o4-mini':'o4-mini','anthropic/claude-sonnet-4-6':'Sonnet 4.6','anthropic/claude-sonnet-4-5':'Sonnet 4.5','anthropic/claude-haiku-3-5':'Haiku 3.5','google/gemini-2.5-pro':'Gemini 2.5 Pro','deepseek/deepseek-chat-v3-0324':'DeepSeek V3','meta-llama/llama-4-scout':'Llama 4 Scout'};
  if(STATIC_LABELS[modelId]) return STATIC_LABELS[modelId];
  return modelId.split('/').pop()||'Unknown';
}

function renderMd(raw){
  let s=raw||'';
  // Pre-pass: convert safe inline HTML tags the model may emit into their
  // markdown equivalents so the pipeline can render them correctly.
  // Only runs OUTSIDE fenced code blocks and backtick spans (stash + restore).
  // Unsafe tags (anything not in the allowlist) are left as-is and will be
  // HTML-escaped by esc() when they reach an innerHTML assignment -- no XSS risk.
  const fence_stash=[];
  s=s.replace(/(```[\s\S]*?```|`[^`\n]+`)/g,m=>{fence_stash.push(m);return '\x00F'+(fence_stash.length-1)+'\x00';});
  // Safe tag → markdown equivalent (these produce the same output as **text** etc.)
  s=s.replace(/<strong>([\s\S]*?)<\/strong>/gi,(_,t)=>'**'+t+'**');
  s=s.replace(/<b>([\s\S]*?)<\/b>/gi,(_,t)=>'**'+t+'**');
  s=s.replace(/<em>([\s\S]*?)<\/em>/gi,(_,t)=>'*'+t+'*');
  s=s.replace(/<i>([\s\S]*?)<\/i>/gi,(_,t)=>'*'+t+'*');
  s=s.replace(/<code>([^<]*?)<\/code>/gi,(_,t)=>'`'+t+'`');
  s=s.replace(/<br\s*\/?>/gi,'\n');
  // Restore stashed code blocks
  s=s.replace(/\x00F(\d+)\x00/g,(_,i)=>fence_stash[+i]);
  // Mermaid blocks: render as diagram containers (processed after DOM insertion)
  s=s.replace(/```mermaid\n?([\s\S]*?)```/g,(_,code)=>{
    const id='mermaid-'+Math.random().toString(36).slice(2,10);
    return `<div class="mermaid-block" data-mermaid-id="${id}">${esc(code.trim())}</div>`;
  });
  s=s.replace(/```([\w+-]*)\n?([\s\S]*?)```/g,(_,lang,code)=>{const h=lang?`<div class="pre-header">${esc(lang)}</div>`:'';return `${h}<pre><code>${esc(code.replace(/\n$/,''))}</code></pre>`;});
  s=s.replace(/`([^`\n]+)`/g,(_,c)=>`<code>${esc(c)}</code>`);
  // inlineMd: process bold/italic/code/links within a single line of text.
  // Used inside list items and blockquotes where the text may already contain
  // HTML from the pre-pass → bold pipeline, so we cannot call esc() directly.
  function inlineMd(t){
    t=t.replace(/\*\*\*(.+?)\*\*\*/g,(_,x)=>`<strong><em>${esc(x)}</em></strong>`);
    t=t.replace(/\*\*(.+?)\*\*/g,(_,x)=>`<strong>${esc(x)}</strong>`);
    t=t.replace(/\*([^*\n]+)\*/g,(_,x)=>`<em>${esc(x)}</em>`);
    t=t.replace(/`([^`\n]+)`/g,(_,x)=>`<code>${esc(x)}</code>`);
    t=t.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g,(_,lb,u)=>`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(lb)}</a>`);
    // Escape any plain text that isn't already wrapped in a tag we produced
    // by escaping bare < > that aren't part of our own tags
    const SAFE_INLINE=/^<\/?(strong|em|code|a)([\s>]|$)/i;
    t=t.replace(/<\/?[a-z][^>]*>/gi,tag=>SAFE_INLINE.test(tag)?tag:esc(tag));
    return t;
  }
  s=s.replace(/\*\*\*(.+?)\*\*\*/g,(_,t)=>`<strong><em>${esc(t)}</em></strong>`);
  s=s.replace(/\*\*(.+?)\*\*/g,(_,t)=>`<strong>${esc(t)}</strong>`);
  s=s.replace(/\*([^*\n]+)\*/g,(_,t)=>`<em>${esc(t)}</em>`);
  s=s.replace(/^### (.+)$/gm,(_,t)=>`<h3>${inlineMd(t)}</h3>`).replace(/^## (.+)$/gm,(_,t)=>`<h2>${inlineMd(t)}</h2>`).replace(/^# (.+)$/gm,(_,t)=>`<h1>${inlineMd(t)}</h1>`);
  s=s.replace(/^---+$/gm,'<hr>');
  s=s.replace(/^> (.+)$/gm,(_,t)=>`<blockquote>${inlineMd(t)}</blockquote>`);
  // B8: improved list handling supporting up to 2 levels of indentation
  s=s.replace(/((?:^(?:  )?[-*+] .+\n?)+)/gm,block=>{
    const lines=block.trimEnd().split('\n');
    let html='<ul>';
    for(const l of lines){
      const indent=/^ {2,}/.test(l);
      const text=l.replace(/^ {0,4}[-*+] /,'');
      if(indent) html+=`<li style="margin-left:16px">${inlineMd(text)}</li>`;
      else html+=`<li>${inlineMd(text)}</li>`;
    }
    return html+'</ul>';
  });
  s=s.replace(/((?:^(?:  )?\d+\. .+\n?)+)/gm,block=>{
    const lines=block.trimEnd().split('\n');
    let html='<ol>';
    for(const l of lines){
      const text=l.replace(/^ {0,4}\d+\. /,'');
      html+=`<li>${inlineMd(text)}</li>`;
    }
    return html+'</ol>';
  });
  s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g,(_,label,url)=>`<a href="${esc(url)}" target="_blank" rel="noopener">${esc(label)}</a>`);
  // Tables: | col | col | header row followed by | --- | --- | separator then data rows
  s=s.replace(/((?:^\|.+\|\n?)+)/gm,block=>{
    const rows=block.trim().split('\n').filter(r=>r.trim());
    if(rows.length<2)return block;
    const isSep=r=>/^\|[\s|:-]+\|$/.test(r.trim());
    if(!isSep(rows[1]))return block;
    const parseRow=r=>r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<td>${esc(c.trim())}</td>`).join('');
    const parseHeader=r=>r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<th>${esc(c.trim())}</th>`).join('');
    const header=`<tr>${parseHeader(rows[0])}</tr>`;
    const body=rows.slice(2).map(r=>`<tr>${parseRow(r)}</tr>`).join('');
    return `<table><thead>${header}</thead><tbody>${body}</tbody></table>`;
  });
  // Escape any remaining HTML tags that are NOT from our own markdown output.
  // Our pipeline only emits: <strong>,<em>,<code>,<pre>,<h1-6>,<ul>,<ol>,<li>,
  // <table>,<thead>,<tbody>,<tr>,<th>,<td>,<hr>,<blockquote>,<p>,<br>,<a>,
  // <div class="..."> (mermaid/pre-header). Everything else is untrusted input.
  const SAFE_TAGS=/^<\/?(strong|em|code|pre|h[1-6]|ul|ol|li|table|thead|tbody|tr|th|td|hr|blockquote|p|br|a|div)([\s>]|$)/i;
  s=s.replace(/<\/?[a-z][^>]*>/gi,tag=>SAFE_TAGS.test(tag)?tag:esc(tag));
  const parts=s.split(/\n{2,}/);
  s=parts.map(p=>{p=p.trim();if(!p)return '';if(/^<(h[1-6]|ul|ol|pre|hr|blockquote)/.test(p))return p;return `<p>${p.replace(/\n/g,'<br>')}</p>`;}).join('\n');
  return s;
}

function setStatus(t){
  const bar=$('activityBar');
  const txt=$('activityText');
  const dismiss=$('btnDismissStatus');
  if(!bar||!txt)return;
  if(!t){
    bar.style.display='none';
    txt.textContent='';
    if(dismiss)dismiss.style.display='none';
  } else {
    txt.textContent=t;
    bar.style.display='';
    // Show dismiss X only for static/error messages, not transient busy ones
    const transient = t.endsWith('…') || t === 'Hermes is thinking…';
    if(dismiss)dismiss.style.display=(!transient && !S.busy)?'inline':'none';
  }
}
function setBusy(v){
  S.busy=v;
  $('btnSend').disabled=v;
  const dots=$('activityDots');
  if(dots) dots.style.display=v?'flex':'none';
  if(!v){
    setStatus('');
    // Always hide Cancel button when not busy
    const _cb=$('btnCancel');if(_cb)_cb.style.display='none';
    updateQueueBadge();
    // Drain one queued message after UI settles
    if(MSG_QUEUE.length>0){
      const next=MSG_QUEUE.shift();
      updateQueueBadge();
      setTimeout(()=>{ $('msg').value=next; send(); }, 120);
    }
  }
}

function updateQueueBadge(){
  let badge=$('queueBadge');
  if(MSG_QUEUE.length>0){
    if(!badge){
      badge=document.createElement('div');
      badge.id='queueBadge';
      badge.style.cssText='position:fixed;bottom:80px;right:24px;background:rgba(124,185,255,.18);border:1px solid rgba(124,185,255,.4);color:var(--blue);font-size:12px;font-weight:600;padding:6px 14px;border-radius:20px;z-index:50;pointer-events:none;backdrop-filter:blur(8px);';
      document.body.appendChild(badge);
    }
    badge.textContent=MSG_QUEUE.length===1?'1 message queued':`${MSG_QUEUE.length} messages queued`;
  } else {
    if(badge) badge.remove();
  }
}
function showToast(msg,ms){const el=$('toast');el.textContent=msg;el.classList.add('show');clearTimeout(el._t);el._t=setTimeout(()=>el.classList.remove('show'),ms||2800);}

function copyMsg(btn){
  const row=btn.closest('.msg-row');
  const text=row?row.dataset.rawText:'';
  if(!text)return;
  navigator.clipboard.writeText(text).then(()=>{
    const orig=btn.innerHTML;btn.innerHTML='<i class="fas fa-check"></i>';btn.style.color='var(--blue)';
    setTimeout(()=>{btn.innerHTML=orig;btn.style.color='';},1500);
  }).catch(()=>showToast('Copy failed'));
}

// ── Reconnect banner (B4/B5: reload resilience) ──
const INFLIGHT_KEY = 'hermes-webui-inflight'; // localStorage key for in-flight session tracking

function markInflight(sid, streamId) {
  localStorage.setItem(INFLIGHT_KEY, JSON.stringify({sid, streamId, ts: Date.now()}));
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
async function refreshSession() {
  dismissReconnect();
  if (!S.session) return;
  try {
    const data = await api(`/api/session?session_id=${encodeURIComponent(S.session.session_id)}`);
    S.session = data.session;
    S.messages = (data.session.messages || []).filter(m => {
      if (!m || !m.role || m.role === 'tool') return false;
      if (m.role === 'assistant') { let c = m.content || ''; if (Array.isArray(c)) c = c.map(p => p.text||'').join(''); return String(c).trim().length > 0; }
      return true;
    });
    syncTopbar(); renderMessages();
    showToast('Conversation refreshed');
  } catch(e) { setStatus('Refresh failed: ' + e.message); }
}
async function checkInflightOnBoot(sid) {
  const raw = localStorage.getItem(INFLIGHT_KEY);
  if (!raw) return;
  try {
    const {sid: inflightSid, streamId, ts} = JSON.parse(raw);
    if (inflightSid !== sid) { clearInflight(); return; }
    // Only show banner if the in-flight entry is less than 10 minutes old
    if (Date.now() - ts > 10 * 60 * 1000) { clearInflight(); return; }
    // Check if stream is still active
    const status = await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId || '')}`);
    if (status.active) {
      // Stream is genuinely still running -- show the banner
      showReconnectBanner('A response is still being generated. Reload when ready?');
    } else {
      // Stream finished. Only show banner if reload happened within 90 seconds
      // (longer gap = normal completed session, not a mid-stream reload)
      if (Date.now() - ts < 90 * 1000) {
        showReconnectBanner('A response was in progress when you last left. Messages may have updated.');
      } else {
        clearInflight();  // completed normally, no banner needed
      }
    }
  } catch(e) { clearInflight(); }
}

// ── Context indicator (in composer, next to attach) ──
function _syncContextIndicator(){
  const el=$('ctxIndicator');
  if(!el) return;
  const u=S.usage;
  if(!u||!u.last_prompt_tokens){el.style.display='none';return;}
  el.style.display='';
  const used=u.last_prompt_tokens;
  const max=u.context_length||200000; // fallback to 200k if unknown
  const pct=Math.min(used/max,1);
  const bar=$('ctxBar');
  const lbl=$('ctxLabel');
  bar.style.width=Math.max(pct*100,2)+'%';
  // Color coding: green <50%, yellow 50-80%, red >80%
  bar.className='ctx-bar '+(pct<0.5?'ctx-low':pct<0.8?'ctx-mid':'ctx-high');
  lbl.textContent=_fmtTokens(used)+'/'+_fmtTokens(max);
  // Update title with details
  const costStr=u.estimated_cost_usd?` | $${u.estimated_cost_usd.toFixed(u.estimated_cost_usd<0.01?4:2)}`:'';
  el.title=`Context: ${used.toLocaleString()} / ${max.toLocaleString()} tokens (${(pct*100).toFixed(1)}%)${costStr}\nClick to compress context`;
}

function sendCompress(){
  if(S.busy) return;
  const ta=$('msg');
  if(ta){ta.value='/compress';send();}
}

function syncTopbar(){
  if(!S.session){
    document.title='Hermes';
    const renameBtn=$('btnRenameSession');
    if(renameBtn) renameBtn.style.display='none';
    // Show default workspace name even without a session
    const sidebarName=$('sidebarWsName');
    if(sidebarName && sidebarName.textContent==='Workspace'){
      sidebarName.textContent='No workspace';
    }
    return;
  }
  const sessionTitle=S.session.title||'Untitled';
  $('topbarTitle').textContent=sessionTitle;
  document.title=sessionTitle+' \u2014 Hermes';
  const renameBtn=$('btnRenameSession');
  if(renameBtn) renameBtn.style.display='';
  // Show auto-title wand only when session title is still "Untitled" and has messages
  const autoTitleBtn=$('btnAutoTitle');
  if(autoTitleBtn){
    const hasMessages=S.messages&&S.messages.filter(m=>m&&m.role&&m.role!=='tool').length>0;
    autoTitleBtn.style.display=(sessionTitle==='Untitled'&&hasMessages)?'':'none';
  }
  const vis=S.messages.filter(m=>m&&m.role&&m.role!=='tool');
  const ts=(S.session.updated_at||S.session.created_at||0)*1000;
  const relTime=ts?_relTime(ts):'';
  $('topbarMeta').textContent=`${vis.length} messages${relTime?' \u00b7 '+relTime:''}`;
  // Update context indicator in composer
  _syncContextIndicator();
  const m=S.session.model||'';
  $('modelSelect').value=m;  // set dropdown first so chip reads consistent value
  // If session model isn't in the dropdown, add it dynamically
  if(m && $('modelSelect').value!==m){
    const opt=document.createElement('option');
    opt.value=m;
    opt.textContent=getModelLabel(m);
    $('modelSelect').appendChild(opt);
    $('modelSelect').value=m;
  }
  // Show Clear button only when session has messages
  const clearBtn=$('btnClearConv');
  if(clearBtn) clearBtn.style.display=(S.messages&&S.messages.filter(msg=>msg.role!=='tool').length>0)?'':'none';
  syncModelCSelect();
  const ws=S.session.workspace||'';
  const wsFriendly=getWorkspaceFriendlyName(ws);

  // Update sidebar workspace custom selector label
  const sidebarName=$('sidebarWsName');
  if(sidebarName) sidebarName.textContent=wsFriendly||ws.split('/').pop()||'Workspace';
  // modelSelect already set above
}

async function triggerAutoTitle(){
  if(!S.session||S.session.title!=='Untitled')return;
  const btn=$('btnAutoTitle');
  if(btn){
    btn.innerHTML='<i class="fas fa-spinner fa-spin"></i>';
    btn.style.opacity='1';
    btn.style.pointerEvents='none';
  }
  const restoreBtn=()=>{
    if(btn){
      btn.innerHTML='<i class="fas fa-wand-magic-sparkles"></i>';
      btn.style.opacity='';
      btn.style.pointerEvents='';
    }
  };
  try{
    await api('/api/session/generate-title',{method:'POST',body:JSON.stringify({session_id:S.session.session_id})});
    showToast('Generating title...');
    // Poll for title update -- the generation is async (runs in background thread)
    let tries=0;
    const poll=setInterval(async()=>{
      tries++;
      if(tries>20){clearInterval(poll);restoreBtn();return;}
      try{
        const data=await api(`/api/session?session_id=${encodeURIComponent(S.session.session_id)}`);
        if(data.session&&data.session.title&&data.session.title!=='Untitled'){
          S.session.title=data.session.title;
          syncTopbar(); // hides btnAutoTitle via display:none logic
          const cached=_allSessions.find(s=>s.session_id===S.session.session_id);
          if(cached)cached.title=data.session.title;
          refreshSessionList();
          clearInterval(poll);
        }
      }catch(e){clearInterval(poll);restoreBtn();}
    },1500);
  }catch(e){
    setStatus('Auto-title failed: '+e.message);
    restoreBtn();
  }
}

// ── Panel collapse toggles ────────────────────────────────────────────────────
function toggleSidebar(){
  const sidebar=document.querySelector('.sidebar');
  if(!sidebar)return;
  const collapsed=sidebar.classList.toggle('collapsed');
  localStorage.setItem('hermes-sidebar-collapsed',collapsed?'1':'0');
}

function toggleRightPanel(){
  const rp=document.querySelector('.rightpanel');
  if(!rp)return;
  const collapsed=rp.classList.toggle('collapsed');
  localStorage.setItem('hermes-rightpanel-collapsed',collapsed?'1':'0');
}

function msgContent(m){
  // Extract plain text content from a message for filtering
  let c=m.content||'';
  if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('').trim();
  return String(c).trim();
}

function renderMessages(){
  const inner=$('msgInner');
  const vis=S.messages.filter(m=>{
    if(!m||!m.role||m.role==='tool')return false;
    return msgContent(m)||m.attachments?.length;
  });
  $('emptyState').style.display=vis.length?'none':'';
  inner.innerHTML='';
  // Track original indices (in S.messages) so truncate knows the cut point
  const visWithIdx=[];
  let rawIdx=0;
  for(const m of S.messages){
    if(!m||!m.role||m.role==='tool'){rawIdx++;continue;}
    if(msgContent(m)||m.attachments?.length) visWithIdx.push({m,rawIdx});
    rawIdx++;
  }
  for(let vi=0;vi<visWithIdx.length;vi++){
    const {m,rawIdx}=visWithIdx[vi];
    let content=m.content||'';
    if(Array.isArray(content))content=content.filter(p=>p&&p.type==='text').map(p=>p.text||p.content||'').join('\n');
    const isUser=m.role==='user';
    const isLastAssistant=!isUser&&vi===visWithIdx.length-1;
    // Detect consecutive assistant messages for visual grouping
    const prevM=vi>0?visWithIdx[vi-1].m:null;
    const nextM=vi<visWithIdx.length-1?visWithIdx[vi+1].m:null;
    const isContinued=!isUser&&prevM&&prevM.role==='assistant';  // not first in a run
    const isLastInRun=!isUser&&(!nextM||nextM.role!=='assistant'); // last in a run
    const row=document.createElement('div');
    let rowClass='msg-row '+(isUser?'user-row':'assistant-row');
    if(isContinued) rowClass+=' assistant-continued';
    row.className=rowClass;
    row.dataset.msgIdx=rawIdx;
    let filesHtml='';
    if(m.attachments&&m.attachments.length)
      filesHtml=`<div class="msg-files">${m.attachments.map(f=>`<div class="msg-file-badge">&#128206; ${esc(f)}</div>`).join('')}</div>`;
    const bodyHtml = isUser ? esc(String(content)).replace(/\n/g,'<br>') : renderMd(String(content));
    // Action buttons for this bubble
    const editBtn  = isUser  ? `<button class="msg-action-btn" title="Edit message" onclick="editMessage(this)"><i class="fas fa-pen"></i></button>` : '';
    const retryBtn = isLastAssistant ? `<button class="msg-action-btn" title="Regenerate response" onclick="regenerateResponse(this)"><i class="fas fa-rotate-right"></i></button>` : '';
    const tsVal=m._ts||m.timestamp;
    const tsTitle=tsVal?new Date(tsVal*1000).toLocaleString():'';
    // If this is the last assistant message overall, always show the full
    // Hermes header (teal) even if it's in the middle of a consecutive run --
    // that's where the regenerate button lives and it must be discoverable.
    const forceFullHeader = isLastAssistant && !isUser;
    const roleHtml=(isContinued && !forceFullHeader)
      // Continued assistant message: hide avatar/name, only show copy button on last in run
      ? `<div class="msg-role ${m.role} msg-role-hidden"><span class="msg-actions">${isLastInRun?`<button class="msg-copy-btn msg-action-btn" title="Copy" onclick="copyMsg(this)"><i class="fas fa-copy"></i></button>`:''}${retryBtn}</span></div>`
      // First message in run, last message overall, or user: full header
      : `<div class="msg-role ${m.role}" ${tsTitle?`title="${esc(tsTitle)}"`:''}><div class="role-icon ${m.role}">${isUser?'Y':'H'}</div><span style="font-size:12px">${isUser?'You':'Hermes'}</span>${tsTitle?`<span class="msg-time">${new Date(tsVal*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</span>`:''}<span class="msg-actions">${editBtn}<button class="msg-copy-btn msg-action-btn" title="Copy" onclick="copyMsg(this)"><i class="fas fa-copy"></i></button>${retryBtn}</span></div>`;
    row.innerHTML=`${roleHtml}${filesHtml}<div class="msg-body">${bodyHtml}</div>`;
    row.dataset.rawText = String(content).trim();
    inner.appendChild(row);
  }
  // Insert tool call cards into the correct position relative to assistant messages.
  // During streaming, tool cards are appended directly to #msgInner by SSE handlers.
  // On done, renderMessages() rebuilds everything from S.messages + S.toolCalls.
  if(!S.busy && S.toolCalls && S.toolCalls.length){
    inner.querySelectorAll('.tool-card-row').forEach(el=>el.remove());
    const byAssistant = {};
    for(const tc of S.toolCalls){
      const key = tc.assistant_msg_idx !== undefined ? tc.assistant_msg_idx : -1;
      if(!byAssistant[key]) byAssistant[key] = [];
      byAssistant[key].push(tc);
    }
    const allRows = Array.from(inner.querySelectorAll('.msg-row[data-msg-idx]'));
    for(const [key, cards] of Object.entries(byAssistant)){
      const aIdx = parseInt(key);
      let insertBefore = null;
      if(aIdx === -1){
        // Fallback: last assistant row
        for(let i=allRows.length-1;i>=0;i--){
          const ri=parseInt(allRows[i].dataset.msgIdx||'-1',10);
          if(ri>=0&&S.messages[ri]&&S.messages[ri].role==='assistant'){insertBefore=allRows[i];break;}
        }
      } else {
        // Find the assistant DOM row at exactly aIdx (the one that contained the tool_use calls).
        // If that message had no text (tool-only turn, filtered from visWithIdx), look for the
        // NEXT assistant row after aIdx instead. This ensures tool cards always appear before
        // the assistant text that followed them, never after.
        for(const r of allRows){
          const ri=parseInt(r.dataset.msgIdx||'-1');
          if(ri>=aIdx && S.messages[ri] && S.messages[ri].role==='assistant'){insertBefore=r;break;}
        }
        // Final fallback: last assistant row (handles edge cases)
        if(!insertBefore){
          for(let i=allRows.length-1;i>=0;i--){
            const ri=parseInt(allRows[i].dataset.msgIdx||'-1',10);
            if(ri>=0&&S.messages[ri]&&S.messages[ri].role==='assistant'){insertBefore=allRows[i];break;}
          }
        }
      }
      const frag=document.createDocumentFragment();
      for(const tc of cards){frag.appendChild(buildToolCard(tc));}
      if(insertBefore) inner.insertBefore(frag,insertBefore);
      else inner.appendChild(frag);
    }
  }
  scrollToBottom();
  // Apply syntax highlighting after DOM is built
  requestAnimationFrame(()=>{highlightCode();renderMermaidBlocks();});
  // Refresh todo panel if it's currently open
  if(typeof loadTodos==='function' && document.getElementById('panelTodos') && document.getElementById('panelTodos').classList.contains('active')){
    loadTodos();
  }
}

function toolIcon(name){
  const icons={terminal:'⬛',read_file:'📄',write_file:'✏️',search_files:'🔍',
    web_search:'🌐',web_extract:'🌐',execute_code:'⚙️',patch:'🔧',
    memory:'🧠',skill_manage:'📚',skill_view:'📚',skills_list:'📚',
    todo:'✅',cronjob:'⏱️',delegate_task:'🤖',session_search:'🔍',
    send_message:'💬',browser_navigate:'🌐',vision_analyze:'👁️',
    honcho_conclude:'🧠',honcho_context:'🧠',honcho_search:'🧠',honcho_profile:'🧠'};
  return icons[name]||'🔧';
}

function buildToolCard(tc){
  const row=document.createElement('div');
  row.className='msg-row tool-card-row';
  const icon=toolIcon(tc.name);
  const hasArgs=tc.args&&Object.keys(tc.args).length>0;
  const hasSnippet=!!tc.snippet;
  const hasDetail=hasSnippet||hasArgs;
  // Build a meaningful preview for the collapsed card header
  const _extracted=hasSnippet?_extractToolOutput(tc.snippet, tc.name):null;
  let previewText=tc.preview||'';
  let previewHtml=null; // if set, overrides previewText with raw html in the header
  let headerExitBadge='';
  if(!previewText){
    // Show the command/input, not the output
    const a=tc.args||{};
    if(tc.name==='execute_code'&&a.code){
      // Show first non-comment, non-blank line of code as preview
      const firstLine=(a.code.split('\n').find(l=>l.trim()&&!l.trim().startsWith('#'))||a.code).trim();
      previewText=firstLine.slice(0,100);
    } else if(tc.name==='terminal'&&a.command){
      previewText=a.command.slice(0,100);
    } else if(tc.name==='read_file'&&a.path){
      previewText=a.path.slice(0,100);
    } else if(tc.name==='search_files'&&a.pattern){
      previewText=`/${a.pattern.slice(0,80)}`+(a.path&&a.path!=='.'?' in '+a.path:'');
    } else if(tc.name==='web_search'&&a.query){
      previewText=a.query.slice(0,100);
    } else if(tc.name==='web_extract'&&a.urls){
      const urls=Array.isArray(a.urls)?a.urls:[a.urls];
      previewText=urls[0]?String(urls[0]).replace(/^https?:\/\//,'').slice(0,100):'';
    } else if((tc.name==='write_file'||tc.name==='read_file')&&a.path){
      previewText=a.path.slice(0,100);
    } else if(tc.name==='todo'){
      // Build mini progress bar from summary or todos array
      const todos=(_extracted&&_extracted.todos)||a.todos||[];
      if(todos.length>0){
        const total=todos.length;
        const done=todos.filter(t=>t.status==='completed').length;
        const pct=Math.round(done/total*100);
        const inprog=todos.filter(t=>t.status==='in_progress').length;
        previewHtml=`<span class="todo-preview-bar" style="display:flex;align-items:center;gap:6px;overflow:hidden;flex:1;min-width:0">` +
          `<span style="flex:1;height:4px;border-radius:2px;background:rgba(255,255,255,.12);overflow:hidden">` +
          `<span style="display:block;height:100%;width:${pct}%;background:var(--teal);border-radius:2px;transition:width .3s"></span>` +
          `</span>` +
          `<span style="font-size:10px;color:var(--muted);opacity:.7;white-space:nowrap">${done}/${total}${inprog>0?` · ${inprog} active`:''}</span>` +
          `</span>`;
      }
    } else if(tc.name==='patch'&&a.path){
      previewText=a.path.slice(0,100);
    } else if(tc.name==='browser_navigate'&&a.url){
      previewText=String(a.url).replace(/^https?:\/\//,'').slice(0,100);
    } else if((tc.name==='browser_click'||tc.name==='browser_type')&&a.ref){
      previewText=a.ref+(a.text?' -> '+String(a.text).slice(0,60):'');
    } else if(tc.name==='browser_snapshot'){
      previewText=a.full?'full page':'interactive elements';
    } else if(tc.name==='browser_vision'&&a.question){
      previewText=a.question.slice(0,100);
    } else if(tc.name==='vision_analyze'&&(a.image_url||a.question)){
      previewText=(a.question||a.image_url||'').slice(0,100);
    } else if((tc.name==='honcho_conclude'||tc.name==='honcho_context'||tc.name==='honcho_search')&&(a.conclusion||a.query||a.question)){
      previewText=(a.conclusion||a.query||a.question||'').slice(0,100);
    } else if(tc.name==='memory'&&a.content){
      previewText=a.content.slice(0,100);
    } else if(tc.name==='send_message'&&a.message){
      previewText=a.message.slice(0,100);
    } else if(tc.name==='delegate_task'&&(a.goal||a.context)){
      previewText=(a.goal||a.context||'').slice(0,100);
    } else if(tc.name==='session_search'){
      previewText=a.query?('"'+String(a.query).slice(0,90)+'"'):(a.limit?`last ${a.limit} sessions`:'recent sessions');
    } else if(tc.name==='skill_view'&&a.name){
      previewText=a.name+(a.file_path?' / '+a.file_path:'');
    } else if(tc.name==='skill_manage'&&(a.name||a.action)){
      previewText=(a.action?a.action+': ':'')+(a.name||'');
    } else if(tc.name==='skills_list'){
      previewText=a.category?'category: '+a.category:'all skills';
    } else if(tc.snippet){
      // Last-resort: try to extract something meaningful from JSON output
      const snip=tc.snippet.trim();
      if(snip.startsWith('{')||snip.startsWith('[')){
        try{
          const obj=JSON.parse(snip);
          // Pick the most meaningful short field
          const label=obj.title||obj.name||obj.path||obj.url||obj.content?.slice?.(0,80)||
                       obj.result?.slice?.(0,80)||obj.message||obj.error||null;
          previewText=label?String(label).slice(0,100):snip.slice(0,60)+'…';
        }catch(e){previewText=snip.slice(0,100);}
      } else {
        previewText=snip.split('\n')[0].slice(0,100);
      }
    }
  }
  // Show exit code badge on collapsed header ONLY for non-zero (failures)
  if(_extracted&&_extracted.isTerminal&&_extracted.exitCode!==''&&_extracted.exitCode!==0&&tc.done!==false){
    headerExitBadge=`<span class="tool-header-exit exit-err">${esc(String(_extracted.exitCode))}</span>`;
  }
  const runIndicator=tc.done===false?'<span class="tool-card-running-dot"></span>':'';

  // Build result snippet HTML (reuse _extracted from preview)
  let snippetHtml='';
  if(hasSnippet){
    const extracted=_extracted;
    if(extracted&&extracted.isDiff){
      // Diff result -- render as color-coded diff view; hide old_string/new_string in advanced
      const filesBadge=extracted.files.map(f=>{
        const fname=f.replace(/.*\//,'');
        return `<span class="diff-file-badge tool-file-link" title="${esc(f)}" onclick="_prOpenVscode(${_prSet(f)},event)">${esc(fname)}</span>`;
      }).join('');
      const lintBadge=extracted.lint&&extracted.lint.status&&extracted.lint.status!=='ok'&&extracted.lint.status!=='skipped'
        ?`<span class="diff-lint-err">lint: ${esc(extracted.lint.status)}</span>`:'';
      const diffLines=extracted.text.split('\n').map(l=>{
        if(l.startsWith('+++') || l.startsWith('---')) return `<div class="diff-line diff-header">${esc(l)}</div>`;
        if(l.startsWith('@@')) return `<div class="diff-line diff-hunk">${esc(l)}</div>`;
        if(l.startsWith('+')) return `<div class="diff-line diff-add"><span class="diff-sign">+</span>${esc(l.slice(1))}</div>`;
        if(l.startsWith('-')) return `<div class="diff-line diff-del"><span class="diff-sign">-</span>${esc(l.slice(1))}</div>`;
        return `<div class="diff-line diff-ctx">${esc(l)}</div>`;
      }).join('');
      snippetHtml=`<div class="tool-card-result">
        <div class="diff-meta">${filesBadge}${lintBadge}</div>
        <div class="diff-view">${diffLines}</div>
      </div>`;
    } else if(extracted&&extracted.isTodoHtml){
      // Todo list -- render as proper HTML with FontAwesome icons
      const statusIcon={
        pending:'<i class="fas fa-circle" style="font-size:8px;color:var(--muted);opacity:.5"></i>',
        in_progress:'<i class="fas fa-circle-notch" style="color:var(--blue)"></i>',
        completed:'<i class="fas fa-check-circle" style="color:var(--teal)"></i>',
        cancelled:'<i class="fas fa-times-circle" style="color:var(--muted);opacity:.4"></i>',
      };
      const items=(extracted.todos||[]).map(t=>{
        const st=t.status||'pending';
        const icon2=statusIcon[st]||statusIcon.pending;
        return `<li class="todo-item todo-item--${esc(st)}">
          <span class="todo-item-icon">${icon2}</span>
          <span class="todo-item-content">${esc(t.content||t.id||'')}<span class="todo-item-id"> #${esc(t.id||'')}</span></span>
        </li>`;
      }).join('');
      const summary=extracted.text?`<div class="todo-summary-line">${esc(extracted.text)}</div>`:'';
      snippetHtml=`<div class="tool-card-result">${summary}<ul class="todo-list">${items}</ul></div>`;
    } else if(extracted){
      // Render clean extracted output
      const full=extracted.text;
      const termClass=extracted.isTerminal?' tool-result-terminal':'';
      const isError=(extracted.isTerminal&&extracted.exitCode!==''&&extracted.exitCode!==0)||extracted.isSoftError;
      const exitBadge=isError?`<span class="tool-exit-code tool-exit-error">exit ${esc(String(extracted.exitCode))}</span>`:'';
      // Make file paths clickable for read_file / search_files
      const pathArg=tc.args&&(tc.args.path||tc.args.urls);
      const pathLink=(tc.name==='read_file'||tc.name==='write_file')&&tc.args&&tc.args.path
        ?`<span class="tool-file-link" onclick="_prOpenVscode(${_prSet(tc.args.path)},event)">${esc(tc.args.path)}</span>`
        :'';
      snippetHtml=`<div class="tool-card-result" data-exit-error="${isError?'1':''}">
        ${pathLink?`<div style="margin-bottom:4px;">${pathLink}</div>`:''}
        ${exitBadge}
        <pre class="tool-card-result-pre${termClass}">${esc(full)}</pre>
      </div>`;
    } else {
      snippetHtml=`<div class="tool-card-result">
        <pre class="tool-card-result-pre">${fmtSnippet(tc.snippet)}</pre>
      </div>`;
    }
  }

  // Build "Show advanced" section containing raw args (replaces old argsHtml at top)
  let advancedHtml='';
  if(hasArgs){
    const uid='adv_'+Math.random().toString(36).slice(2);
    const rows=Object.entries(tc.args).map(([k,v])=>{
      const valHtml=fmtArgVal(v);
      const isMultiLine=typeof v==='object'||(typeof v==='string'&&v.length>80);
      return isMultiLine
        ? `<div class="tool-arg-row tool-arg-row--block"><span class="tool-arg-key">${esc(k)}</span><pre class="tool-arg-val tool-arg-val--pre">${valHtml}</pre></div>`
        : `<div class="tool-arg-row"><span class="tool-arg-key">${esc(k)}</span><span class="tool-arg-val">${valHtml}</span></div>`;
    }).join('');
    advancedHtml=`<div class="tool-advanced-toggle" onclick="(function(el){const b=el.nextElementSibling;b.classList.toggle('open');el.querySelector('i').className=b.classList.contains('open')?'fas fa-chevron-down':'fas fa-chevron-right';})(this)"><i class="fas fa-chevron-right"></i> Show advanced</div><div class="tool-advanced-body"><div class="tool-card-args">${rows}</div></div>`;
  }

  const _isErr=_extracted&&(
    (_extracted.isTerminal&&_extracted.exitCode!==''&&_extracted.exitCode!==0)||
    _extracted.isSoftError
  );
  row.innerHTML=`
    <div class="tool-card${tc.done===false?' tool-card-running':''}${_isErr?' tool-card-error':''}">
      <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
        ${runIndicator}
        <span class="tool-card-icon">${icon}</span>
        <span class="tool-card-name">${esc(tc.name)}</span>
        ${headerExitBadge}
        ${previewHtml!==null?previewHtml:`<span class="tool-card-preview">${esc(previewText)}</span>`}
        ${hasDetail?'<span class="tool-card-toggle">▸</span>':''}
      </div>
      ${hasDetail?`<div class="tool-card-detail">${snippetHtml}${advancedHtml}</div>`:''}
    </div>`;
  if(tc.tid) row.dataset.tid=tc.tid;
  return row;
}

// Open a file path in VS Code from tool card click
// Path registry: avoid putting raw JSON.stringify strings in html onclick attrs
// (double-quotes in JSON break the attribute boundary).
// Usage: _pathReg.set(idx, path); onclick="_prCall('fn',idx,event)"
const _pathReg = new Map();
let _pathRegIdx = 0;
function _prSet(path){ const i=_pathRegIdx++; _pathReg.set(i,path); return i; }
function _prGet(i){ return _pathReg.get(i)||''; }
window._prOpenVscode=(i,e)=>_openFileInVscode(_prGet(i),e);
window._prGitPull=(i,btn)=>gitPull(_prGet(i),btn);

async function _openFileInVscode(path, event){
  if(event){event.stopPropagation();event.preventDefault();}
  if(!S.session) return;
  try{
    await api('/api/file/open-in-vscode',{method:'POST',body:JSON.stringify({session_id:S.session.session_id, path})});
    showToast('Opening in VS Code...');
  }catch(e){ setStatus('VS Code open failed: '+e.message); }
}

// ── Edit + Regenerate ──

function editMessage(btn) {
  if(S.busy) return;
  const row = btn.closest('.msg-row');
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
    if(e.key==='Enter' && !e.shiftKey) { e.preventDefault(); bar.querySelector('.msg-edit-send').click(); }
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
  } catch(e) { setStatus('Edit failed: ' + e.message); }
}

async function regenerateResponse(btn) {
  if(!S.session || S.busy) return;
  // Find the last user message and re-run it
  // Remove the last assistant message first (truncate to before it)
  const row = btn.closest('.msg-row');
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
  } catch(e) { setStatus('Regenerate failed: ' + e.message); }
}

function highlightCode(container) {
  // Apply Prism.js syntax highlighting to all code blocks in container (or whole messages area)
  if(typeof Prism === 'undefined' || !Prism.highlightAllUnder) return;
  const el = container || $('msgInner');
  if(!el) return;
  Prism.highlightAllUnder(el);
}

let _mermaidLoading=false;
let _mermaidReady=false;

function renderMermaidBlocks(){
  const blocks=document.querySelectorAll('.mermaid-block:not([data-rendered])');
  if(!blocks.length) return;
  if(!_mermaidReady){
    if(!_mermaidLoading){
      _mermaidLoading=true;
      const script=document.createElement('script');
      script.src='https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js';
      script.onload=()=>{
        if(typeof mermaid!=='undefined'){
          mermaid.initialize({startOnLoad:false,theme:'dark',themeVariables:{
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
      block.innerHTML=svg;
      block.classList.add('mermaid-rendered');
    }catch(e){
      // Fall back to showing as a code block
      block.innerHTML=`<div class="pre-header">mermaid</div><pre><code>${esc(code)}</code></pre>`;
    }
  });
}

function appendThinking(){
  $('emptyState').style.display='none';
  const row=document.createElement('div');row.className='msg-row';row.id='thinkingRow';
  row.innerHTML=`<div class="msg-role assistant"><div class="role-icon assistant">H</div>Hermes</div><div class="thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>`;
  $('msgInner').appendChild(row);scrollToBottom();
}
function removeThinking(){const el=$('thinkingRow');if(el)el.remove();}

function fileIcon(name, type){
  if(type==='dir') return '📁';
  const e=fileExt(name);
  if(IMAGE_EXTS.has(e)) return '📷';
  if(MD_EXTS.has(e))    return '📝';
  if(typeof DOWNLOAD_EXTS!=='undefined'&&DOWNLOAD_EXTS.has(e)) return '⬇️';
  if(e==='.py')   return '🐍';
  if(e==='.js'||e==='.ts'||e==='.jsx'||e==='.tsx') return '⚡';
  if(e==='.json'||e==='.yaml'||e==='.yml'||e==='.toml') return '⚙';
  if(e==='.sh'||e==='.bash') return '💻';
  if(e==='.pdf') return '⬇️';
  return '📄';
}

// ── Folder label storage (server-persisted, workspace-scoped) ──
// In-memory cache: {workspace: {path: label}}
let _labelCache = null;
let _labelCacheWs = null;

async function _ensureLabelCache(){
  const ws=S.session&&S.session.workspace;
  if(!ws) return;
  if(_labelCache && _labelCacheWs===ws) return;
  try{
    const d=await api(`/api/workspace/labels?workspace=${encodeURIComponent(ws)}`);
    _labelCache=d.labels||{};_labelCacheWs=ws;
  }catch(e){_labelCache=_labelCache||{};_labelCacheWs=ws;}
}

function getFolderLabel(path){
  if(!_labelCache||!path) return '';
  return _labelCache[path]||'';
}

function setFolderLabel(path, label){
  if(!S.session||!path) return;
  const ws=S.session.workspace;
  if(!_labelCache||_labelCacheWs!==ws){_labelCache={};_labelCacheWs=ws;}
  if(label.trim()) _labelCache[path]=label.trim();
  else delete _labelCache[path];
  // Fire-and-forget persist to server
  api('/api/workspace/label',{method:'POST',body:JSON.stringify({workspace:ws,path,label:label.trim()})}).catch(()=>{});
}
function getFileTags(path){
  if(!S.session || !path) return [];
  const key=`fileTags-${S.session.session_id}-${path}`;
  const stored=localStorage.getItem(key)||'';
  return stored ? stored.split(',').map(t=>t.trim()).filter(t=>t) : [];
}
function setFileTags(path, tags){
  if(!S.session || !path) return;
  const key=`fileTags-${S.session.session_id}-${path}`;
  const normalized = Array.isArray(tags) ? tags : (typeof tags==='string' ? tags.split(',').map(t=>t.trim()) : []);
  const filtered = normalized.filter(t=>t.trim()).map(t=>t.trim());
  if(filtered.length) localStorage.setItem(key, filtered.join(','));
  else localStorage.removeItem(key);
}

function renderFileTree(){
  const box=$('fileTree');box.innerHTML='';
  const nowSec2=Date.now()/1000;
  // Sort entries: git repos by last_commit_ts desc (recent first), then non-git dirs, then files
  const sorted=[...S.entries].sort((a,b)=>{
    const aIsGit=a.type==='dir'&&a.is_git;
    const bIsGit=b.type==='dir'&&b.is_git;
    if(aIsGit&&bIsGit) return (b.last_commit_ts||0)-(a.last_commit_ts||0);
    if(aIsGit) return -1; if(bIsGit) return 1;
    if(a.type==='dir'&&b.type==='file') return -1;
    if(a.type==='file'&&b.type==='dir') return 1;
    return a.name.localeCompare(b.name);
  });
  // Split git repos into active and inactive (>90 days)
  const activeItems=sorted.filter(i=>!i.is_git||(nowSec2-i.last_commit_ts)/86400<=90||!i.last_commit_ts);
  const inactiveItems=sorted.filter(i=>i.is_git&&i.last_commit_ts&&(nowSec2-i.last_commit_ts)/86400>90);
  let _inactiveSectionOpen=false;

  const renderItem=(item)=>{
    const el=document.createElement('div');el.className='file-item';

    // Icon
    const iconEl=document.createElement('span');
    iconEl.className='file-icon';iconEl.textContent=fileIcon(item.name,item.type);
    el.appendChild(iconEl);

    // Name -- takes all remaining space, truncates with ellipsis
    const nameEl=document.createElement('span');
    nameEl.className='file-name';nameEl.textContent=item.name;
    el.appendChild(nameEl);

    // Label -- folder: FA tag icon on hover; shows text badge when label is set
    // Rendered BEFORE git badge so git icon is always the rightmost element
    if(item.type==='dir'){
      const curLabel=getFolderLabel(item.path);
      const labelEl=document.createElement('span');
      labelEl.className='folder-label'+(curLabel?'':' folder-label--empty');
      labelEl.innerHTML=curLabel
        ?`<i class="fas fa-tag"></i> ${esc(curLabel)}`
        :'<i class="fas fa-tag"></i>';
      labelEl.title=curLabel?'Click to edit label':'Click to add label';
      labelEl.onclick=(e)=>{e.stopPropagation();editFolderLabel(item.path,labelEl);};
      el.appendChild(labelEl);
    }

    // Git badge -- folders that are git repos get a branch icon colored by activity age
    // Always appended last so it stays on the far right
    if(item.type==='dir' && item.is_git){
      const gitEl=document.createElement('span');
      gitEl.className='folder-git-badge';
      // Color by days since last commit using theme colors
      const nowSec=Date.now()/1000;
      const ageDays=item.last_commit_ts?((nowSec-item.last_commit_ts)/86400):999;
      let gitColor;
      if(ageDays<=5)       gitColor='var(--teal)';
      else if(ageDays<=10) gitColor='var(--green-dull)';
      else if(ageDays<=30) gitColor='var(--gold)';
      else if(ageDays<=90) gitColor='var(--accent)';
      else                 gitColor='rgba(168,168,168,0.35)';  // very stale -- muted grey, visually unimportant
      gitEl.style.color=gitColor;
      const behindCount=item.git_behind||0;
      gitEl.innerHTML=behindCount>0
        ?`<i class="fas fa-code-branch"></i><span class="git-behind-pill" title="${behindCount} incoming commit${behindCount>1?'s':''}">${behindCount}</span>`
        :'<i class="fas fa-code-branch"></i>';
      gitEl.title=behindCount>0?`Git repo · ${behindCount} incoming commit${behindCount>1?'s':''}  — click for details`:'Git repository — click for details';
      gitEl.onclick=(e)=>{e.stopPropagation();openGitModal(item.path,item.name);};
      el.appendChild(gitEl);
    }

    // Tags -- file: FA tags icon on hover; shows text badge when tags are set
    if(item.type==='file'){
      const curTags=getFileTags(item.path);
      const tagsEl=document.createElement('span');
      tagsEl.className='file-tags'+(curTags.length?'':' file-tags--empty');
      tagsEl.innerHTML=curTags.length
        ?`<i class="fas fa-tags"></i> ${esc(curTags.join(', '))}`
        :'<i class="fas fa-tags"></i>';
      tagsEl.title=curTags.length?'Click to edit tags':'Click to add tags';
      tagsEl.onclick=(e)=>{e.stopPropagation();editFileTags(item.path,tagsEl);};
      el.appendChild(tagsEl);
    }

    // Size -- human readable (B / KB / MB)
    if(item.type==='file' && item.size != null){
      const sizeEl=document.createElement('span');
      sizeEl.className='file-size';
      const sz=item.size;
      sizeEl.textContent=sz<1024?`${sz} B`:sz<1024*1024?`${(sz/1024).toFixed(1)} KB`:`${(sz/(1024*1024)).toFixed(1)} MB`;
      el.appendChild(sizeEl);
    }

    el.onclick=async()=>item.type==='dir'?loadDir(item.path):openFile(item.path);
    return el;
  };

  // Render active items
  for(const item of activeItems) box.appendChild(renderItem(item));

  // Render inactive section (git repos > 90 days)
  if(inactiveItems.length){
    const sectionHdr=document.createElement('div');
    sectionHdr.style.cssText='font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:rgba(120,120,120,0.5);padding:8px 8px 3px;cursor:pointer;user-select:none;display:flex;align-items:center;gap:4px;';
    const caretEl=document.createElement('i');
    caretEl.className=_inactiveSectionOpen?'fas fa-chevron-down':'fas fa-chevron-right';
    caretEl.style.cssText='font-size:8px;';
    sectionHdr.appendChild(caretEl);
    sectionHdr.appendChild(document.createTextNode('Inactive ('+inactiveItems.length+')'));
    const sectionBody=document.createElement('div');
    sectionBody.style.display=_inactiveSectionOpen?'':'none';
    sectionHdr.onclick=()=>{
      _inactiveSectionOpen=!_inactiveSectionOpen;
      caretEl.className=_inactiveSectionOpen?'fas fa-chevron-down':'fas fa-chevron-right';
      sectionBody.style.display=_inactiveSectionOpen?'':'none';
    };
    box.appendChild(sectionHdr);
    for(const item of inactiveItems){
      const el=renderItem(item);
      el.style.opacity='0.55';
      sectionBody.appendChild(el);
    }
    box.appendChild(sectionBody);
  }
}

function editFolderLabel(path, labelEl){
  const curLabel=getFolderLabel(path);
  const inp=document.createElement('input');
  inp.className='folder-label-input';
  inp.type='text';inp.maxLength='50';inp.placeholder='Label (optional)';
  inp.value=curLabel;
  inp.onclick=(e)=>e.stopPropagation();
  inp.onkeydown=(e)=>{
    if(e.key==='Enter'){e.preventDefault();saveFolderLabel();}
    if(e.key==='Escape'){e.preventDefault();cancelFolderLabel();}
  };
  inp.onblur=()=>saveFolderLabel();
  const saveFolderLabel=()=>{
    const newLabel=inp.value.trim();
    setFolderLabel(path,newLabel);
    labelEl.className='folder-label'+(newLabel?'':' folder-label--empty');
    labelEl.innerHTML=newLabel?`<i class="fas fa-tag"></i> ${esc(newLabel)}`:'<i class="fas fa-tag"></i>';
    labelEl.title=newLabel?'Click to edit label':'Click to add label';
    inp.replaceWith(labelEl);
  };
  const cancelFolderLabel=()=>{
    inp.replaceWith(labelEl);
  };
  labelEl.replaceWith(inp);
  inp.focus();inp.select();
}

function editFileTags(path, tagsEl){
  const curTags=getFileTags(path);
  const inp=document.createElement('input');
  inp.className='file-tags-input';
  inp.type='text';inp.maxLength='200';inp.placeholder='Tags, comma-separated (optional)';
  inp.value=curTags.join(', ');
  inp.onclick=(e)=>e.stopPropagation();
  inp.onkeydown=(e)=>{
    if(e.key==='Enter'){e.preventDefault();saveFileTags();}
    if(e.key==='Escape'){e.preventDefault();cancelFileTags();}
  };
  inp.onblur=()=>saveFileTags();
  const saveFileTags=()=>{
    const newTags=inp.value.split(',').map(t=>t.trim()).filter(t=>t);
    setFileTags(path,newTags);
    tagsEl.className='file-tags'+(newTags.length?'':' file-tags--empty');
    tagsEl.innerHTML=newTags.length?`<i class="fas fa-tags"></i> ${esc(newTags.join(', '))}`:'<i class="fas fa-tags"></i>';
    tagsEl.title=newTags.length?'Click to edit tags':'Click to add tags';
    inp.replaceWith(tagsEl);
  };
  const cancelFileTags=()=>{
    inp.replaceWith(tagsEl);
  };
  tagsEl.replaceWith(inp);
  inp.focus();inp.select();
}

async function openGitModal(repoPath, repoName){
  const modal=$('gitModal');
  if(!modal) return;
  const body=$('gitModalBody');
  body.innerHTML='<div class="git-modal-loading"><i class="fas fa-circle-notch fa-spin"></i> Loading git info...</div>';
  $('gitModalTitle').textContent=repoName;
  modal.style.display='flex';
  document.body.style.overflow='hidden';

  let info;
  try{
    info=await api(`/api/git-info?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(repoPath)}`);
  }catch(e){
    body.innerHTML=`<div class="git-modal-err"><i class="fas fa-circle-exclamation"></i> ${esc(e.message)}</div>`;
    return;
  }

  const aheadBehind=info.ahead||info.behind
    ?`<span class="git-pill git-ahead" title="Commits ahead of remote"><i class="fas fa-arrow-up"></i> ${info.ahead}</span><span class="git-pill git-behind" title="Commits behind remote"><i class="fas fa-arrow-down"></i> ${info.behind}</span>`
    :'<span class="git-pill git-clean">up to date</span>';

  const statusBadges=[
    info.modified  ?`<span class="git-pill git-mod" title="Modified">${info.modified} modified</span>`:'',
    info.added     ?`<span class="git-pill git-add" title="Staged">${info.added} staged</span>`:'',
    info.untracked ?`<span class="git-pill git-unt" title="Untracked">${info.untracked} untracked</span>`:'',
    info.deleted   ?`<span class="git-pill git-del" title="Deleted">${info.deleted} deleted</span>`:'',
  ].filter(Boolean).join('') || '<span class="git-pill git-clean">clean</span>';

  // Branch link -- if we have a remote, link to the branch on the remote
  const branchUrl=info.remote_url&&info.branch&&info.branch!=='HEAD'
    ?`${info.remote_url}/tree/${encodeURIComponent(info.branch)}`
    :null;
  const branchEl=branchUrl
    ?`<a class="git-branch-name git-link" href="${branchUrl}" target="_blank" rel="noopener"><i class="fas fa-code-branch"></i> ${esc(info.branch)}</a>`
    :`<span class="git-branch-name"><i class="fas fa-code-branch"></i> ${esc(info.branch||'(detached)')}</span>`;

  // Commits table
  const commits=info.commits&&info.commits.length?info.commits:[];
  const commitRows=commits.map(c=>{
    const commitUrl=info.remote_url?`${info.remote_url}/commit/${c.hash}`:null;
    const hashEl=commitUrl
      ?`<a class="git-commit-hash git-link" href="${commitUrl}" target="_blank" rel="noopener">${esc(c.hash)}</a>`
      :`<span class="git-commit-hash">${esc(c.hash)}</span>`;
    return `<tr><td class="git-td-hash">${hashEl}</td><td class="git-td-subject">${esc(c.subject)}</td><td class="git-td-meta">${esc(c.rel)}</td></tr>`;
  }).join('');
  const commitsHtml=commitRows
    ?`<table class="git-commits-table"><tbody>${commitRows}</tbody></table>`
    :'<div class="git-log-line git-muted">No commits</div>';

  const stashHtml=info.stashes.length
    ?`<div class="git-section-label" style="margin-top:8px">Stashes (${info.stashes.length})</div>`+info.stashes.map(l=>`<div class="git-log-line git-muted">${esc(l)}</div>`).join('')
    :'';

  // Sync section -- branch + ahead/behind + pull button
  const pullBtn=info.behind>0
    ?`<button class="git-pull-btn" onclick="_prGitPull(${_prSet(repoPath)},this)"><i class="fas fa-arrow-down"></i> Pull ${info.behind} commit${info.behind!==1?'s':''}</button>`
    :'';
  const pushNote=info.ahead>0
    ?`<span class="git-push-disabled" title="Push manually in terminal"><i class="fas fa-ban"></i> ${info.ahead} commit${info.ahead!==1?'s':''} ahead (push manually)</span>`
    :'';

  const workingTreeHtml=info.status_lines.length
    ?`<div class="git-log">${info.status_lines.map(l=>`<div class="git-log-line">${esc(l)}</div>`).join('')}</div>`
    :`<span class="git-status-clean"><i class="fas fa-check-circle"></i> Clean working tree</span>`;

  body.innerHTML=`
    <div class="git-section-block">
      <div class="git-section-title">Branch &amp; Sync</div>
      <div class="git-row" style="flex-wrap:wrap;gap:8px">
        ${branchEl}
        <span style="margin-left:auto;display:flex;gap:6px;align-items:center">${pullBtn}${pushNote}${!pullBtn&&!pushNote?'<span class="git-pill git-clean">up to date</span>':''}</span>
      </div>
    </div>
    <div class="git-section-block">
      <div class="git-section-title">Working Tree</div>
      <div class="git-row" style="flex-wrap:wrap;gap:4px;margin-bottom:6px">${statusBadges}</div>
      ${workingTreeHtml}
    </div>
    <div class="git-section-block">
      <div class="git-section-title">Recent Commits</div>
      ${commitsHtml}
    </div>
    ${info.stashes.length?`<div class="git-section-block"><div class="git-section-title">Stashes (${info.stashes.length})</div>${info.stashes.map(l=>`<div class="git-log-line git-muted">${esc(l)}</div>`).join('')}</div>`:''}
  `;
}

async function gitPull(repoPath, btn){
  btn.disabled=true;btn.innerHTML='<i class="fas fa-spinner fa-spin"></i> Pulling...';
  try{
    const res=await api('/api/git-pull',{method:'POST',body:JSON.stringify({path:repoPath,session_id:S.session&&S.session.session_id})});
    if(res.ok) btn.innerHTML='<i class="fas fa-check"></i> Done';
    else btn.innerHTML='<i class="fas fa-exclamation-triangle"></i> '+esc(res.error||'Failed');
    // Reload git modal + explorer after short delay
    setTimeout(()=>{
      const title=$('gitModalTitle').textContent;
      openGitModal(repoPath,title);
      // Refresh explorer so the file tree reflects pulled changes
      if(typeof loadDir==='function'&&typeof _currentDir!=='undefined'){
        _lastDirWorkspace=null; // bust guard so loadDir re-fetches
        loadDir(_currentDir||'.');
      }
    },1200);
  }catch(e){btn.innerHTML='Error: '+esc(e.message);btn.disabled=false;}
}

async function promptNewFile(){
  if(!S.session)return;
  const name=prompt('New file name (e.g. notes.md):','');
  if(!name||!name.trim())return;
  try{
    await api('/api/file/create',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:name.trim(),content:''})});
    showToast(`Created ${name.trim()}`);
    await loadDir('.');
    // Open the new file immediately
    openFile(name.trim());
  }catch(e){setStatus('Create failed: '+e.message);}
}

async function promptNewFolder(){
  if(!S.session)return;
  const name=prompt('New folder name:','');
  if(!name||!name.trim())return;
  try{
    await api('/api/file/create-dir',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:name.trim()})});
    showToast(`Created folder ${name.trim()}`);
    await loadDir('.');
  }catch(e){setStatus('Create folder failed: '+e.message);}
}

function renderTray(){
  const tray=$('attachTray');tray.innerHTML='';
  if(!S.pendingFiles.length){tray.classList.remove('has-files');return;}
  tray.classList.add('has-files');
  S.pendingFiles.forEach((f,i)=>{
    const chip=document.createElement('div');chip.className='attach-chip';
    chip.innerHTML=`&#128206; ${esc(f.name)} <button title="Remove">&#10005;</button>`;
    chip.querySelector('button').onclick=()=>{S.pendingFiles.splice(i,1);renderTray();};
    tray.appendChild(chip);
  });
}
function addFiles(files){for(const f of files){if(!S.pendingFiles.find(p=>p.name===f.name))S.pendingFiles.push(f);}renderTray();}

async function uploadPendingFiles(){
  if(!S.pendingFiles.length||!S.session)return[];
  const names=[];let failures=0;
  const bar=$('uploadBar');const barWrap=$('uploadBarWrap');
  barWrap.classList.add('active');bar.style.width='0%';
  const total=S.pendingFiles.length;
  for(let i=0;i<total;i++){
    const f=S.pendingFiles[i];const fd=new FormData();
    fd.append('session_id',S.session.session_id);fd.append('file',f,f.name);
    try{
      const res=await fetch('/api/upload',{method:'POST',body:fd,credentials:'include'});
      if(!res.ok){const err=await res.text();throw new Error(err);}
      const data=await res.json();
      if(data.error)throw new Error(data.error);
      names.push(data.filename);
    }catch(e){failures++;setStatus(`\u274c Upload failed: ${f.name} \u2014 ${e.message}`);}
    bar.style.width=`${Math.round((i+1)/total*100)}%`;
  }
  barWrap.classList.remove('active');bar.style.width='0%';
  S.pendingFiles=[];renderTray();
  if(failures===total&&total>0)throw new Error(`All ${total} upload(s) failed`);
  return names;
}

