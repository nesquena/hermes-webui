const S={session:null,messages:[],entries:[],busy:false,pendingFiles:[],toolCalls:[],activeStreamId:null,currentDir:'.',activeProfile:'default'};
const INFLIGHT={};  // keyed by session_id while request in-flight
const MSG_QUEUE=[];  // messages queued while a request is in-flight
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// Dynamic model labels -- populated by populateModelDropdown(), fallback to static map
let _dynamicModelLabels={};

// ── Smart model resolver ────────────────────────────────────────────────────
// Finds the best matching option value in a <select> for a given model ID.
// Handles mismatches like 'claude-sonnet-4-6' vs 'anthropic/claude-sonnet-4.6'.
// Returns the matched option's value (already in the list), or null if no match.
function _findModelInDropdown(modelId, sel){
  if(!modelId||!sel) return null;
  const opts=Array.from(sel.options).map(o=>o.value);
  // 1. Exact match
  if(opts.includes(modelId)) return modelId;
  // 2. Normalize: lowercase, strip namespace prefix, replace hyphens→dots
  const norm=s=>s.toLowerCase().replace(/^[^/]+\//,'').replace(/-/g,'.');
  const target=norm(modelId);
  const exact=opts.find(o=>norm(o)===target);
  if(exact) return exact;
  // 3. Prefix/substring: target starts with or contains a significant chunk
  const base=target.replace(/\.\d+$/,'');  // strip trailing version number
  const partial=opts.find(o=>norm(o).startsWith(base)||norm(o).includes(base));
  return partial||null;
}

// Set the model picker to the best match for modelId.
// Returns the resolved value that was actually set, or null if nothing matched.
function _applyModelToDropdown(modelId, sel){
  if(!modelId||!sel) return null;
  const resolved=_findModelInDropdown(modelId,sel);
  if(resolved){
    sel.value=resolved;
    return resolved;
  }
  return null;
}

async function populateModelDropdown(){
  const sel=$('modelSelect');
  if(!sel) return;
  try{
    const data=await fetch(new URL('/api/models',location.origin).href,{credentials:'include'}).then(r=>r.json());
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
      _applyModelToDropdown(data.default_model, sel);
    }
  }catch(e){
    // API unavailable -- keep the hardcoded HTML options as fallback
    console.warn('Failed to load models from server:',e.message);
  }
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
function _fmtTokens(n){if(!n||n<0)return'0';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'k';return String(n);}

// Context usage indicator in composer footer
function _syncCtxIndicator(usage){
  const el=$('ctxIndicator');
  if(!el)return;
  const promptTok=usage.last_prompt_tokens||usage.input_tokens||0;
  const ctxWindow=usage.context_length||0;
  if(!promptTok||!ctxWindow){el.style.display='none';return;}
  el.style.display='';
  const pct=Math.min(100,Math.round((promptTok/ctxWindow)*100));
  const bar=$('ctxBar');
  const label=$('ctxLabel');
  if(bar){
    bar.style.width=pct+'%';
    bar.className='ctx-bar'+(pct>75?' ctx-high':pct>50?' ctx-mid':'');
  }
  if(label){
    const cost=usage.estimated_cost;
    let text=`${_fmtTokens(promptTok)} / ${_fmtTokens(ctxWindow)}`;
    if(pct>0) text+=` (${pct}%)`;
    if(cost) text+=` \u00b7 $${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
    label.textContent=text;
  }
  // Update title with detailed info
  const threshold=usage.threshold_tokens||0;
  el.title=`Context: ${_fmtTokens(promptTok)} of ${_fmtTokens(ctxWindow)} tokens used`
    +(threshold?`\nAuto-compress at ${_fmtTokens(threshold)} (${Math.round(threshold/ctxWindow*100)}%)`:'');
}

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
  const STATIC_LABELS={'openai/gpt-5.4-mini':'GPT-5.4 Mini','openai/gpt-4o':'GPT-4o','openai/o3':'o3','openai/o4-mini':'o4-mini','anthropic/claude-sonnet-4.6':'Sonnet 4.6','anthropic/claude-sonnet-4-5':'Sonnet 4.5','anthropic/claude-haiku-3-5':'Haiku 3.5','google/gemini-2.5-pro':'Gemini 2.5 Pro','deepseek/deepseek-chat-v3-0324':'DeepSeek V3','meta-llama/llama-4-scout':'Llama 4 Scout'};
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
    const transient = t.endsWith('…') || t === (window._botName||'Hermes')+' is thinking\u2026';
    if(dismiss)dismiss.style.display=(!transient && !S.busy)?'inline':'none';
  }
}
function updateSendBtn(){
  const btn=$('btnSend');
  if(!btn) return;
  const hasContent=$('msg').value.trim().length>0||S.pendingFiles.length>0;
  const shouldShow=hasContent&&!S.busy;
  if(shouldShow&&btn.style.display==='none'){
    btn.style.display='';
    // Remove then re-add class to retrigger animation each time
    btn.classList.remove('visible');
    requestAnimationFrame(()=>btn.classList.add('visible'));
  } else if(!shouldShow&&btn.style.display!=='none'){
    btn.style.display='none';
    btn.classList.remove('visible');
  }
}
function setBusy(v){
  S.busy=v;
  $('btnSend').disabled=v;
  updateSendBtn();
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
    const orig=btn.innerHTML;btn.innerHTML='&#10003;';btn.style.color='var(--blue)';
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
// ── Update banner ──
function _showUpdateBanner(data){
  const parts=[];
  if(data.webui&&data.webui.behind>0) parts.push(`WebUI: ${data.webui.behind} update${data.webui.behind>1?'s':''}`);
  if(data.agent&&data.agent.behind>0) parts.push(`Agent: ${data.agent.behind} update${data.agent.behind>1?'s':''}`);
  if(!parts.length)return;
  const msg=$('updateMsg');
  if(msg) msg.textContent='\u2B06 '+parts.join(', ')+' available';
  const banner=$('updateBanner');
  if(banner) banner.classList.add('visible');
  window._updateData=data;
}
function dismissUpdate(){
  const b=$('updateBanner');if(b)b.classList.remove('visible');
  sessionStorage.setItem('hermes-update-dismissed','1');
}
async function applyUpdates(){
  const btn=$('btnApplyUpdate');
  if(btn){btn.disabled=true;btn.textContent='Updating\u2026';}
  const targets=[];
  if(window._updateData?.webui?.behind>0) targets.push('webui');
  if(window._updateData?.agent?.behind>0) targets.push('agent');
  try{
    for(const target of targets){
      const res=await api('/api/updates/apply',{method:'POST',body:JSON.stringify({target})});
      if(!res.ok){
        showToast('Update failed ('+target+'): '+(res.message||'unknown error'));
        if(btn){btn.disabled=false;btn.textContent='Update Now';}
        return;
      }
    }
    showToast('Updated! Reloading\u2026');
    sessionStorage.removeItem('hermes-update-checked');
    sessionStorage.removeItem('hermes-update-dismissed');
    setTimeout(()=>location.reload(),1500);
  }catch(e){
    showToast('Update failed: '+e.message);
    if(btn){btn.disabled=false;btn.textContent='Update Now';}
  }
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

function syncTopbar(){
  if(!S.session){
    document.title=window._botName||'Hermes';
    // Show default workspace name even without a session
    const sidebarName=$('sidebarWsName');
    if(sidebarName && sidebarName.textContent==='Workspace'){
      sidebarName.textContent='No workspace';
    }
    return;
  }
  const sessionTitle=S.session.title||'Untitled';
  $('topbarTitle').textContent=sessionTitle;
  document.title=sessionTitle+' \u2014 '+(window._botName||'Hermes');
  const vis=S.messages.filter(m=>m&&m.role&&m.role!=='tool');
  $('topbarMeta').textContent=`${vis.length} messages`;
  // If a profile switch just happened, apply its model rather than the session's stale value.
  // S._pendingProfileModel is set by switchToProfile() and cleared here after one application.
  const modelOverride=S._pendingProfileModel;
  if(modelOverride){
    S._pendingProfileModel=null;
    _applyModelToDropdown(modelOverride,$('modelSelect'));
  } else {
    const m=S.session.model||'';
    const applied=_applyModelToDropdown(m,$('modelSelect'));
    // If the model isn't in the list at all, add it so the session value is preserved
    if(!applied && m){
      const opt=document.createElement('option');
      opt.value=m;
      opt.textContent=getModelLabel(m);
      $('modelSelect').appendChild(opt);
      $('modelSelect').value=m;
    }
  }
  // Show Clear button only when session has messages
  const clearBtn=$('btnClearConv');
  if(clearBtn) clearBtn.style.display=(S.messages&&S.messages.filter(msg=>msg.role!=='tool').length>0)?'':'none';
  const displayModel=$('modelSelect').value||m;
  $('modelChip').textContent=getModelLabel(displayModel);
  const ws=S.session.workspace||'';
  // Update sidebar workspace display
  const sidebarName=$('sidebarWsName');
  const sidebarPath=$('sidebarWsPath');
  if(sidebarName){
    sidebarName.textContent=getWorkspaceFriendlyName(ws);
  }
  if(sidebarPath){
    sidebarPath.textContent=ws;
  }
  // modelSelect already set above
  // Update profile chip label
  const profileLabel=$('profileChipLabel');
  if(profileLabel) profileLabel.textContent=S.activeProfile||'default';
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
    // Extract thinking/reasoning blocks from structured content (Claude extended thinking, o3)
    let thinkingText='';
    if(Array.isArray(content)){
      thinkingText=content.filter(p=>p&&(p.type==='thinking'||p.type==='reasoning')).map(p=>p.thinking||p.reasoning||p.text||'').join('\n');
      content=content.filter(p=>p&&p.type==='text').map(p=>p.text||p.content||'').join('\n');
    }
    const isUser=m.role==='user';
    const isLastAssistant=!isUser&&vi===visWithIdx.length-1;
    // Render thinking card before the assistant message (collapsed by default)
    if(thinkingText&&!isUser){
      const thinkRow=document.createElement('div');thinkRow.className='msg-row thinking-card-row';
      thinkRow.innerHTML=`<div class="thinking-card"><div class="thinking-card-header" onclick="this.parentElement.classList.toggle('open')"><span class="thinking-card-icon">&#128161;</span><span class="thinking-card-label">Thinking</span><span class="thinking-card-toggle">&#9656;</span></div><div class="thinking-card-body"><pre>${esc(thinkingText)}</pre></div></div>`;
      inner.appendChild(thinkRow);
    }
    const row=document.createElement('div');row.className='msg-row';
    row.dataset.msgIdx=rawIdx;row.dataset.role=m.role||'assistant';
    let filesHtml='';
    if(m.attachments&&m.attachments.length)
      filesHtml=`<div class="msg-files">${m.attachments.map(f=>`<div class="msg-file-badge">&#128206; ${esc(f)}</div>`).join('')}</div>`;
    const bodyHtml = isUser ? esc(String(content)).replace(/\n/g,'<br>') : renderMd(String(content));
    // Action buttons for this bubble
    const editBtn  = isUser  ? `<button class="msg-action-btn" title="Edit message" onclick="editMessage(this)">&#9998;</button>` : '';
    const retryBtn = isLastAssistant ? `<button class="msg-action-btn" title="Regenerate response" onclick="regenerateResponse(this)">&#8635;</button>` : '';
    const tsVal=m._ts||m.timestamp;
    const tsTitle=tsVal?new Date(tsVal*1000).toLocaleString():'';
    const _bn=window._botName||'Hermes';
    row.innerHTML=`<div class="msg-role ${m.role}" ${tsTitle?`title="${esc(tsTitle)}"`:''}><div class="role-icon ${m.role}">${isUser?'Y':esc(_bn.charAt(0).toUpperCase())}</div><span style="font-size:12px">${isUser?'You':esc(_bn)}</span>${tsTitle?`<span class="msg-time">${new Date(tsVal*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</span>`:''}<span class="msg-actions">${editBtn}<button class="msg-copy-btn msg-action-btn" title="Copy" onclick="copyMsg(this)">&#128203;</button>${retryBtn}</span></div>${filesHtml}<div class="msg-body">${bodyHtml}</div>`;
    row.dataset.rawText = String(content).trim();
    inner.appendChild(row);
  }
  // Insert settled tool call cards (history view only).
  // During live streaming, tool cards are rendered in #liveToolCards by the
  // tool SSE handler and never mixed into the message list until done fires.
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
      // Find the right insertion point: cards go AFTER the assistant message
      // that triggered them. We look for the row at aIdx, or the nearest
      // visible row at or before aIdx (the assistant message may be filtered
      // out if it contained only tool_use blocks with no text response).
      let anchorRow = null;
      if(aIdx >= 0){
        // First: exact match for the assistant row
        for(const r of allRows){
          const ri=parseInt(r.dataset.msgIdx||'-1');
          if(ri===aIdx){anchorRow=r;break;}
        }
        // Fallback: nearest visible row at or before aIdx
        if(!anchorRow){
          for(let i=allRows.length-1;i>=0;i--){
            const ri=parseInt(allRows[i].dataset.msgIdx||'-1');
            if(ri<=aIdx){anchorRow=allRows[i];break;}
          }
        }
      }
      // aIdx === -1: attach after the last assistant row
      if(!anchorRow){
        for(let i=allRows.length-1;i>=0;i--){
          const ri=parseInt(allRows[i].dataset.msgIdx||'-1',10);
          if(ri>=0&&S.messages[ri]&&S.messages[ri].role==='assistant'){anchorRow=allRows[i];break;}
        }
      }
      const frag=document.createDocumentFragment();
      for(const tc of cards){frag.appendChild(buildToolCard(tc));}
      // Add expand/collapse toggle for groups with 2+ cards
      if(cards.length>=2){
        const toggle=document.createElement('div');
        toggle.className='tool-cards-toggle';
        const cardEls=Array.from(frag.querySelectorAll('.tool-card'));
        const expandBtn=document.createElement('button');
        expandBtn.textContent='Expand all';
        expandBtn.onclick=()=>cardEls.forEach(c=>c.classList.add('open'));
        const collapseBtn=document.createElement('button');
        collapseBtn.textContent='Collapse all';
        collapseBtn.onclick=()=>cardEls.forEach(c=>c.classList.remove('open'));
        toggle.appendChild(expandBtn);
        toggle.appendChild(collapseBtn);
        frag.insertBefore(toggle,frag.firstChild);
      }
      // Insert after the anchor row, or append at the end
      if(anchorRow&&anchorRow.nextSibling) inner.insertBefore(frag,anchorRow.nextSibling);
      else inner.appendChild(frag);
    }
  }
  // Render usage badge on the last assistant message row (if enabled and usage data exists)
  if(window._showTokenUsage&&S.session&&(S.session.input_tokens||S.session.output_tokens)){
    const rows=inner.querySelectorAll('.msg-row');
    let lastAssist=null;
    for(let i=rows.length-1;i>=0;i--){if(rows[i].dataset.role==='assistant'){lastAssist=rows[i];break;}}
    if(lastAssist&&!lastAssist.querySelector('.msg-usage')){
      const usage=document.createElement('div');
      usage.className='msg-usage';
      const inTok=S.session.input_tokens||0;
      const outTok=S.session.output_tokens||0;
      const cost=S.session.estimated_cost;
      let text=`${_fmtTokens(inTok)} in · ${_fmtTokens(outTok)} out`;
      if(cost) text+=` · ~$${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
      usage.textContent=text;
      lastAssist.appendChild(usage);
    }
  }
  scrollToBottom();
  // Apply syntax highlighting after DOM is built
  requestAnimationFrame(()=>{highlightCode();addCopyButtons();renderMermaidBlocks();});
  // Refresh todo panel if it's currently open
  if(typeof loadTodos==='function' && document.getElementById('panelTodos') && document.getElementById('panelTodos').classList.contains('active')){
    loadTodos();
  }
}

function toolIcon(name){
  const icons={terminal:'⬛',read_file:'📄',write_file:'✏️',search_files:'🔍',
    web_search:'🌐',web_extract:'🌐',execute_code:'⚙️',patch:'🔧',
    memory:'🧠',skill_manage:'📚',todo:'✅',cronjob:'⏱️',delegate_task:'🤖',
    send_message:'💬',browser_navigate:'🌐',vision_analyze:'👁️',
    subagent_progress:'🔀'};
  return icons[name]||'🔧';
}

function buildToolCard(tc){
  const row=document.createElement('div');
  row.className='msg-row tool-card-row';
  const icon=toolIcon(tc.name);
  const hasDetail=tc.snippet||(tc.args&&Object.keys(tc.args).length>0);
  let displaySnippet='';
  if(tc.snippet){
    const s=tc.snippet;
    if(s.length<=220){displaySnippet=s;}
    else{
      const cutoff=s.slice(0,220);
      const lastBreak=Math.max(cutoff.lastIndexOf('. '),cutoff.lastIndexOf('\n'),cutoff.lastIndexOf('; '));
      displaySnippet=lastBreak>80?s.slice(0,lastBreak+1):cutoff;
    }
  }
  const hasMore=tc.snippet&&tc.snippet.length>displaySnippet.length;
  const runIndicator=tc.done===false?'<span class="tool-card-running-dot"></span>':'';
  const isSubagent=tc.name==='subagent_progress';
  const isDelegation=tc.name==='delegate_task';
  const cardClass='tool-card'+(tc.done===false?' tool-card-running':'')+(isSubagent?' tool-card-subagent':'');
  // Clean up subagent preview: strip leading 🔀 emoji since the icon already shows it
  let displayName=tc.name;
  if(isSubagent) displayName='Subagent';
  if(isDelegation) displayName='Delegate task';
  let previewText=tc.preview||displaySnippet||'';
  if(isSubagent) previewText=previewText.replace(/^🔀\s*/,'');
  row.innerHTML=`
    <div class="${cardClass}">
      <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
        ${runIndicator}
        <span class="tool-card-icon">${icon}</span>
        <span class="tool-card-name">${esc(displayName)}</span>
        <span class="tool-card-preview">${esc(previewText)}</span>
        ${hasDetail?'<span class="tool-card-toggle">▸</span>':''}
      </div>
      ${hasDetail?`<div class="tool-card-detail">
        ${tc.args&&Object.keys(tc.args).length?`<div class="tool-card-args">${
          Object.entries(tc.args).map(([k,v])=>`<div><span class="tool-arg-key">${esc(k)}</span> <span class="tool-arg-val">${esc(String(v))}</span></div>`).join('')
        }</div>`:''}
        ${displaySnippet?`<div class="tool-card-result">
          <pre>${esc(displaySnippet)}</pre>
          ${hasMore?`<button class="tool-card-more" data-full="${esc(tc.snippet||'').replace(/"/g,'&quot;')}" data-short="${esc(displaySnippet||'').replace(/"/g,'&quot;')}" onclick="event.stopPropagation();const p=this.previousElementSibling;const full=this.dataset.full;const short=this.dataset.short;p.textContent=p.textContent===short?full:short;this.textContent=p.textContent===short?'Show more':'Show less'">Show more</button>`:''}
        </div>`:''}
      </div>`:''}
    </div>`;
  return row;
}

// ── Live tool card helpers (called during SSE streaming) ──
function appendLiveToolCard(tc){
  const container=$('liveToolCards');
  if(!container)return;
  container.style.display='';
  // Update existing card if same tool call id (e.g. snippet arrives after done)
  const existing=container.querySelector(`[data-tid="${CSS.escape(tc.tid||'')}"]`);
  if(existing){existing.replaceWith(buildToolCard(tc));return;}
  const card=buildToolCard(tc);
  if(tc.tid)card.dataset.tid=tc.tid;
  container.appendChild(card);
}

function clearLiveToolCards(){
  const container=$('liveToolCards');
  if(!container)return;
  container.innerHTML='';
  container.style.display='none';
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

function addCopyButtons(container){
  const el=container||$('msgInner');
  if(!el) return;
  el.querySelectorAll('pre > code').forEach(codeEl=>{
    const pre=codeEl.parentElement;
    if(pre.querySelector('.code-copy-btn')) return;
    const btn=document.createElement('button');
    btn.className='code-copy-btn';
    btn.textContent='Copy';
    btn.onclick=(e)=>{
      e.stopPropagation();
      navigator.clipboard.writeText(codeEl.textContent).then(()=>{
        btn.textContent='Copied!';
        setTimeout(()=>{btn.textContent='Copy';},1500);
      });
    };
    const header=pre.previousElementSibling;
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

function renderMermaidBlocks(){
  const blocks=document.querySelectorAll('.mermaid-block:not([data-rendered])');
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
    } else {
      seg.className='breadcrumb-seg breadcrumb-current';
    }
    bar.appendChild(seg);
  }
}

// Track expanded directories for tree view
if(!S._expandedDirs) S._expandedDirs=new Set();
// Cache of fetched directory contents: path -> entries[]
if(!S._dirCache) S._dirCache={};

function renderFileTree(){
  const box=$('fileTree');box.innerHTML='';
  // Cache current dir entries
  S._dirCache[S.currentDir||'.']=S.entries;
  _renderTreeItems(box, S.entries, 0);
}

function _renderTreeItems(container, entries, depth){
  for(const item of entries){
    const el=document.createElement('div');el.className='file-item';
    el.style.paddingLeft=(8+depth*16)+'px';

    if(item.type==='dir'){
      // Toggle arrow for directories
      const arrow=document.createElement('span');
      arrow.className='file-tree-toggle';
      const isExpanded=S._expandedDirs.has(item.path);
      arrow.textContent=isExpanded?'\u25BE':'\u25B8';
      el.appendChild(arrow);
    }

    // Icon
    const iconEl=document.createElement('span');
    iconEl.className='file-icon';iconEl.textContent=fileIcon(item.name,item.type);
    el.appendChild(iconEl);

    // Name
    const nameEl=document.createElement('span');
    nameEl.className='file-name';nameEl.textContent=item.name;nameEl.title='Double-click to rename';
    nameEl.ondblclick=(e)=>{
      e.stopPropagation();
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
              showToast(`Renamed to ${newName}`);
              // Invalidate cache and re-render
              delete S._dirCache[S.currentDir];
              await loadDir(S.currentDir);
            }catch(err){showToast('Rename failed: '+err.message);}
          }
        }
        inp.replaceWith(nameEl);
      };
      inp.onkeydown=(e2)=>{
        if(e2.key==='Enter'){e2.preventDefault();finish(true);}
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

    // Delete button -- for files
    if(item.type==='file'){
      const del=document.createElement('button');
      del.className='file-del-btn';del.title='Delete';del.textContent='\u00d7';
      del.onclick=async(e)=>{e.stopPropagation();await deleteWorkspaceFile(item.path,item.name);};
      el.appendChild(del);
    }

    if(item.type==='dir'){
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
      const children=S._dirCache[item.path]||[];
      if(children.length){
        _renderTreeItems(container, children, depth+1);
      }else{
        const empty=document.createElement('div');
        empty.className='file-item file-empty';
        empty.style.paddingLeft=(8+(depth+1)*16)+'px';
        empty.textContent='(empty)';
        container.appendChild(empty);
      }
    }
  }
}

async function deleteWorkspaceFile(relPath, name){
  if(!S.session)return;
  if(!confirm(`Delete ${name}?`))return;
  try{
    await api('/api/file/delete',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath})});
    showToast(`Deleted ${name}`);
    // Close preview if we just deleted the viewed file
    if($('previewPathText').textContent===relPath)$('btnClearPreview').onclick();
    await loadDir(S.currentDir);
  }catch(e){setStatus('Delete failed: '+e.message);}
}

async function promptNewFile(){
  if(!S.session)return;
  const name=prompt('New file name (e.g. notes.md):','');
  if(!name||!name.trim())return;
  const relPath=S.currentDir==='.'?name.trim():(S.currentDir+'/'+name.trim());
  try{
    await api('/api/file/create',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath,content:''})});
    showToast(`Created ${name.trim()}`);
    await loadDir(S.currentDir);
    openFile(relPath);
  }catch(e){setStatus('Create failed: '+e.message);}
}

async function promptNewFolder(){
  if(!S.session)return;
  const name=prompt('New folder name:','');
  if(!name||!name.trim())return;
  const relPath=S.currentDir==='.'?name.trim():(S.currentDir+'/'+name.trim());
  try{
    await api('/api/file/create-dir',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath})});
    showToast(`Created folder ${name.trim()}`);
    await loadDir(S.currentDir);
  }catch(e){setStatus('Create folder failed: '+e.message);}
}

function renderTray(){
  const tray=$('attachTray');tray.innerHTML='';
  if(!S.pendingFiles.length){tray.classList.remove('has-files');updateSendBtn();return;}
  tray.classList.add('has-files');
  updateSendBtn();
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
      const res=await fetch(new URL('/api/upload',location.origin).href,{method:'POST',credentials:'include',body:fd});
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

