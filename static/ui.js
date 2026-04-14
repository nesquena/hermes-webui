const S={session:null,messages:[],entries:[],busy:false,pendingFiles:[],toolCalls:[],activeStreamId:null,currentDir:'.',activeProfile:'default'};
const INFLIGHT={};  // keyed by session_id while request in-flight
const SESSION_QUEUES={};  // keyed by session_id for queued follow-up turns
const $=id=>document.getElementById(id);
function _getSessionQueue(sid, create=false){
  if(!sid) return [];
  if(!SESSION_QUEUES[sid]&&create) SESSION_QUEUES[sid]=[];
  return SESSION_QUEUES[sid]||[];
}
function queueSessionMessage(sid, payload){
  if(!sid||!payload) return 0;
  const q=_getSessionQueue(sid,true);
  q.push(payload);
  return q.length;
}
function shiftQueuedSessionMessage(sid){
  const q=_getSessionQueue(sid,false);
  if(!q.length) return null;
  const next=q.shift();
  if(!q.length) delete SESSION_QUEUES[sid];
  return next;
}
function getQueuedSessionCount(sid){
  return _getSessionQueue(sid,false).length;
}
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
    if(sel.id==='modelSelect' && typeof syncModelChip==='function') syncModelChip();
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
    // Store active provider globally so the send path can warn on mismatch
    window._activeProvider=data.active_provider||null;
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
    if(typeof syncModelChip==='function') syncModelChip();
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

async function _fetchLiveModels(provider, sel){
  if(!provider||!sel) return;
  // Don't fetch for providers where we know it's unsupported or unnecessary
  // All providers now supported via agent's provider_model_ids() — no exclusions needed
  if(_liveModelCache[provider]) return; // already fetched this session
  try{
    const url=new URL('/api/models/live',location.origin);
    url.searchParams.set('provider',provider);
    const data=await fetch(url.href,{credentials:'include'}).then(r=>r.json());
    if(!data.models||!data.models.length) return;
    _liveModelCache[provider]=data.models;
    // Remember current selection before rebuilding options
    const currentVal=sel.value;
    // Rebuild the optgroup for this provider with live models
    // Keep other providers' optgroups intact
    let providerGroup=null;
    for(const og of sel.querySelectorAll('optgroup')){
      if(og.label&&og.label.toLowerCase().includes(provider.toLowerCase())){
        providerGroup=og; break;
      }
    }
    if(!providerGroup){
      // No existing group — add a new one
      providerGroup=document.createElement('optgroup');
      providerGroup.label=provider.charAt(0).toUpperCase()+provider.slice(1)+' (live)';
      sel.appendChild(providerGroup);
    }
    // Rebuild options from live data
    const existingIds=new Set([...sel.options].map(o=>o.value));
    let added=0;
    for(const m of data.models){
      if(existingIds.has(m.id)) continue; // already shown from static list
      const opt=document.createElement('option');
      opt.value=m.id;
      opt.textContent=m.label||m.id;
      opt.title='Live model — fetched from provider';
      providerGroup.appendChild(opt);
      _dynamicModelLabels[m.id]=m.label||m.id;
      added++;
    }
    if(added>0){
      // Restore selection
      if(currentVal) _applyModelToDropdown(currentVal, sel);
      if(typeof syncModelChip==='function') syncModelChip();
      console.log('[hermes] Live models loaded for',provider+':',added,'new models added');
    }
  }catch(e){
    console.debug('[hermes] Live model fetch failed for',provider,e.message);
  }
}

/**
 * Check if the given model ID belongs to a different provider than the one
 * currently configured in Hermes. Returns a warning string if mismatched,
 * or null if the selection looks compatible.
 *
 * Provider detection is intentionally loose — we compare the model's slash
 * prefix (e.g. "openai/" from "openai/gpt-4o") against the active provider
 * name. Custom/local endpoints report active_provider='custom' or the
 * base_url hostname and we skip the check to avoid false positives.
 */
function _checkProviderMismatch(modelId){
  const ap=(window._activeProvider||'').toLowerCase();
  if(!ap||ap==='custom'||ap==='openrouter') return null; // can't reliably check
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

function syncModelChip(){
  const sel=$('modelSelect');
  const chip=$('composerModelChip');
  const label=$('composerModelLabel');
  const dd=$('composerModelDropdown');
  if(!sel||!chip||!label) return;
  const opt=_selectedModelOption();
  label.textContent=opt?opt.textContent:getModelLabel(sel.value||'');
  chip.title=sel.value||'Conversation model';
  chip.classList.toggle('active',!!(dd&&dd.classList.contains('open')));
}

function _positionModelDropdown(){
  const dd=$('composerModelDropdown');
  const chip=$('composerModelChip');
  const footer=document.querySelector('.composer-footer');
  if(!dd||!chip||!footer) return;
  const chipRect=chip.getBoundingClientRect();
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
  dd.innerHTML='';
  for(const child of Array.from(sel.children)){
    if(child.tagName==='OPTGROUP'){
      const heading=document.createElement('div');
      heading.className='model-group';
      heading.textContent=child.label||'Models';
      dd.appendChild(heading);
      for(const opt of Array.from(child.children)){
        const row=document.createElement('div');
        row.className='model-opt'+(opt.value===sel.value?' active':'');
        row.innerHTML=`<span class="model-opt-name">${esc(opt.textContent||getModelLabel(opt.value))}</span><span class="model-opt-id">${esc(opt.value)}</span>`;
        row.onclick=()=>selectModelFromDropdown(opt.value);
        dd.appendChild(row);
      }
      continue;
    }
    if(child.tagName==='OPTION'){
      const row=document.createElement('div');
      row.className='model-opt'+(child.value===sel.value?' active':'');
      row.innerHTML=`<span class="model-opt-name">${esc(child.textContent||getModelLabel(child.value))}</span><span class="model-opt-id">${esc(child.value)}</span>`;
      row.onclick=()=>selectModelFromDropdown(child.value);
      dd.appendChild(row);
    }
  }
}

async function selectModelFromDropdown(value){
  const sel=$('modelSelect');
  if(!sel||sel.value===value) { closeModelDropdown(); return; }
  sel.value=value;
  syncModelChip();
  closeModelDropdown();
  if(typeof sel.onchange==='function') await sel.onchange();
}

function toggleModelDropdown(){
  const dd=$('composerModelDropdown');
  const chip=$('composerModelChip');
  const sel=$('modelSelect');
  if(!dd||!chip||!sel) return;
  const open=dd.classList.contains('open');
  if(open){closeModelDropdown(); return;}
  if(typeof closeProfileDropdown==='function') closeProfileDropdown();
  if(typeof closeWsDropdown==='function') closeWsDropdown();
  renderModelDropdown();
  dd.classList.add('open');
  _positionModelDropdown();
  chip.classList.add('active');
}

function closeModelDropdown(){
  const dd=$('composerModelDropdown');
  const chip=$('composerModelChip');
  if(dd) dd.classList.remove('open');
  if(chip) chip.classList.remove('active');
}

document.addEventListener('click',e=>{
  if(!e.target.closest('#composerModelChip') && !e.target.closest('#composerModelDropdown')) closeModelDropdown();
});
window.addEventListener('resize',()=>{
  const dd=$('composerModelDropdown');
  if(dd&&dd.classList.contains('open')) _positionModelDropdown();
});

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
  const wrap=$('ctxIndicatorWrap');
  const el=$('ctxIndicator');
  if(!el)return;
  const promptTok=usage.last_prompt_tokens||usage.input_tokens||0;
  const totalTok=(usage.input_tokens||0)+(usage.output_tokens||0);
  const ctxWindow=usage.context_length||0;
  const cost=usage.estimated_cost;
  // Show indicator whenever we have any usage data (tokens or cost)
  if(!promptTok&&!totalTok&&!cost){
    if(wrap) wrap.style.display='none';
    return;
  }
  if(wrap) wrap.style.display='';
  const hasCtxWindow=!!(promptTok&&ctxWindow);
  const pct=hasCtxWindow?Math.min(100,Math.round((promptTok/ctxWindow)*100)):0;
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
  if(center) center.textContent=hasCtxWindow?String(pct):'\u00b7';
  el.classList.toggle('ctx-mid',pct>50&&pct<=75);
  el.classList.toggle('ctx-high',pct>75);
  let label=hasCtxWindow?`Context window ${pct}% used`:`${_fmtTokens(totalTok)} tokens used`;
  if(cost) label+=` \u00b7 $${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
  el.setAttribute('aria-label',label);
  if(usageLine) usageLine.textContent=hasCtxWindow?`${pct}% used (${Math.max(0,100-pct)}% left)`:`${_fmtTokens(totalTok)} tokens used`;
  if(tokensLine) tokensLine.textContent=hasCtxWindow?`${_fmtTokens(promptTok)} / ${_fmtTokens(ctxWindow)} tokens used`:`In: ${_fmtTokens(usage.input_tokens||0)} \u00b7 Out: ${_fmtTokens(usage.output_tokens||0)}`;
  const threshold=usage.threshold_tokens||0;
  if(thresholdLine){
    if(threshold&&ctxWindow){
      thresholdLine.style.display='';
      thresholdLine.textContent=`Auto-compress at ${_fmtTokens(threshold)} (${Math.round(threshold/ctxWindow*100)}%)`;
    }else{
      thresholdLine.style.display='none';
      thresholdLine.textContent='';
    }
  }
  if(costLine){
    if(cost){
      costLine.style.display='';
      costLine.textContent=`Estimated cost: $${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
    }else{
      costLine.style.display='none';
      costLine.textContent='';
    }
  }
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
  // Pre-pass: decode HTML entities first so markdown processing works correctly.
  // This prevents double-escaping when LLM outputs entities like &lt; &gt; &amp;
  const decode=s=>s.replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').replace(/&quot;/g,'"').replace(/&#39;/g,"'");
  s=decode(s);
  // Pre-pass: convert safe inline HTML tags the model may emit into their
  // markdown equivalents so the pipeline can render them correctly.
  // Only runs OUTSIDE fenced code blocks and backtick spans (stash + restore).
  // Unsafe tags (anything not in the allowlist) are left as-is and will be
  // HTML-escaped by esc() when they reach an innerHTML assignment -- no XSS risk.
  // Fence stash: protect code blocks and backtick spans from all further processing
  // Must run BEFORE math_stash so $..$ inside code spans is not extracted as math
  const fence_stash=[];
  s=s.replace(/(```[\s\S]*?```|`[^`\n]+`)/g,m=>{fence_stash.push(m);return '\x00F'+(fence_stash.length-1)+'\x00';});
  // Math stash: protect $$..$$ and $..$ from markdown processing
  // Runs AFTER fence_stash so backtick code spans protect their dollar-sign contents
  const math_stash=[];
  // Display math: $$...$$  (must come before inline to avoid mis-parsing)
  s=s.replace(/\$\$([\s\S]+?)\$\$/g,(_,m)=>{math_stash.push({type:'display',src:m});return '\x00M'+(math_stash.length-1)+'\x00';});
  // Inline math: $...$ — require non-space at boundaries to avoid false positives
  // e.g. "costs $5 and $10" should not trigger (space after opening $)
  s=s.replace(/\$([^\s$\n][^$\n]*?[^\s$\n]|\S)\$/g,(_,m)=>{math_stash.push({type:'inline',src:m});return '\x00M'+(math_stash.length-1)+'\x00';});
  // Also stash \(...\) and \[...\] LaTeX delimiters
  s=s.replace(/\\\\\((.+?)\\\\\)/g,(_,m)=>{math_stash.push({type:'inline',src:m});return '\x00M'+(math_stash.length-1)+'\x00';});
  s=s.replace(/\\\\\[(.+?)\\\\\]/gs,(_,m)=>{math_stash.push({type:'display',src:m});return '\x00M'+(math_stash.length-1)+'\x00';});
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
    t=t.replace(/(https?:\/\/[^\s<>"')\]]+)/g,(url)=>{const trail=url.match(/[.,;:!?)]$/)?url.slice(-1):'';const clean=trail?url.slice(0,-1):url;return `<a href="${esc(clean)}" target="_blank" rel="noopener">${esc(clean)}</a>${trail}`;});
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
    const parseRow=r=>r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<td>${inlineMd(c.trim())}</td>`).join('');
    const parseHeader=r=>r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<th>${inlineMd(c.trim())}</th>`).join('');
    const header=`<tr>${parseHeader(rows[0])}</tr>`;
    const body=rows.slice(2).map(r=>`<tr>${parseRow(r)}</tr>`).join('');
    return `<table><thead>${header}</thead><tbody>${body}</tbody></table>`;
  });
  // Escape any remaining HTML tags that are NOT from our own markdown output.
  // Our pipeline only emits: <strong>,<em>,<code>,<pre>,<h1-6>,<ul>,<ol>,<li>,
  // <table>,<thead>,<tbody>,<tr>,<th>,<td>,<hr>,<blockquote>,<p>,<br>,<a>,
  // <div class="..."> (mermaid/pre-header). Everything else is untrusted input.
  const SAFE_TAGS=/^<\/?(strong|em|code|pre|h[1-6]|ul|ol|li|table|thead|tbody|tr|th|td|hr|blockquote|p|br|a|div|span)([\s>]|$)/i;
  s=s.replace(/<\/?[a-z][^>]*>/gi,tag=>SAFE_TAGS.test(tag)?tag:esc(tag));
  // Autolink: convert plain URLs to clickable links (not inside existing <a> tags, not in code)
  s=s.replace(/(https?:\/\/[^\s<>"')\]]+)/g,(url)=>{
    // Strip trailing punctuation that was likely not part of the URL
    const trail=url.match(/[.,;:!?)]$/)?url.slice(-1):'';
    const clean=trail?url.slice(0,-1):url;
    return `<a href="${esc(clean)}" target="_blank" rel="noopener">${esc(clean)}</a>${trail}`;
  });
  // Restore math stash → katex placeholder spans/divs
  // These will be rendered by renderKatexBlocks() after DOM insertion
  s=s.replace(/\x00M(\d+)\x00/g,(_,i)=>{
    const item=math_stash[+i];
    if(item.type==='display'){
      return `<div class="katex-block" data-katex="display">${esc(item.src)}</div>`;
    }
    return `<span class="katex-inline" data-katex="inline">${esc(item.src)}</span>`;
  });
  const parts=s.split(/\n{2,}/);
  s=parts.map(p=>{p=p.trim();if(!p)return '';if(/^<(h[1-6]|ul|ol|pre|hr|blockquote)/.test(p))return p;return `<p>${p.replace(/\n/g,'<br>')}</p>`;}).join('\n');
  return s;
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

function updateSendBtn(){
  const btn=$('btnSend');
  if(!btn) return;
  const hasContent=$('msg').value.trim().length>0||S.pendingFiles.length>0;
  const canSend=hasContent&&!S.busy;
  // Hide while busy (cancel button takes its place); show otherwise
  btn.style.display=S.busy?'none':'';
  btn.disabled=!canSend;
  if(canSend&&!btn.classList.contains('visible')){
    btn.classList.remove('visible');
    requestAnimationFrame(()=>btn.classList.add('visible'));
  }
}
function setBusy(v){
  S.busy=v;
  updateSendBtn();
  if(!v){
    setStatus('');
    setComposerStatus('');
    // Always hide Cancel button when not busy
    const _cb=$('btnCancel');if(_cb)_cb.style.display='none';
    const sid=S.session&&S.session.session_id;
    updateQueueBadge(sid);
    // Drain one queued message for the currently viewed session after UI settles
    const next=sid?shiftQueuedSessionMessage(sid):null;
    if(next){
      updateQueueBadge(sid);
      setTimeout(()=>{
        $('msg').value=next.text||'';
        S.pendingFiles=Array.isArray(next.files)?[...next.files]:[];
        autoResize();
        renderTray();
        send();
      },120);
    }
  }
}

function updateQueueBadge(sessionId){
  const sid=sessionId||(S.session&&S.session.session_id);
  const count=sid?getQueuedSessionCount(sid):0;
  let badge=$('queueBadge');
  if(count>0){
    if(!badge){
      badge=document.createElement('div');
      badge.id='queueBadge';
      badge.style.cssText='position:fixed;bottom:80px;right:24px;background:rgba(124,185,255,.18);border:1px solid rgba(124,185,255,.4);color:var(--blue);font-size:12px;font-weight:600;padding:6px 14px;border-radius:20px;z-index:50;pointer-events:none;backdrop-filter:blur(8px);';
      document.body.appendChild(badge);
    }
    badge.textContent=count===1?'1 message queued':`${count} messages queued`;
  } else if(badge) {
    badge.remove();
  }
}
function showToast(msg,ms){const el=$('toast');el.textContent=msg;el.classList.add('show');clearTimeout(el._t);el._t=setTimeout(()=>el.classList.remove('show'),ms||2800);}

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
    input.value=opts.value||'';input.placeholder=opts.placeholder||'';
    input.autocomplete='off';input.spellcheck=false;
  }
  if(cancelBtn) cancelBtn.textContent=opts.cancelLabel||t('cancel');
  if(confirmBtn){confirmBtn.textContent=opts.confirmLabel||t('create');confirmBtn.classList.remove('danger');}
  if(dialog) dialog.setAttribute('role','dialog');
  if(overlay){overlay.style.display='flex';overlay.setAttribute('aria-hidden','false');}
  return new Promise(resolve=>{
    APP_DIALOG.resolve=resolve;
    setTimeout(()=>{if(input&&input.style.display!=='none')input.focus();else if(confirmBtn)confirmBtn.focus();},0);
  });
}


function copyMsg(btn){
  const row=btn.closest('.msg-row');
  const text=row?row.dataset.rawText:'';
  if(!text)return;
  navigator.clipboard.writeText(text).then(()=>{
    const orig=btn.innerHTML;btn.innerHTML=li('check',13);btn.style.color='var(--blue)';
    setTimeout(()=>{btn.innerHTML=orig;btn.style.color='';},1500);
  }).catch(()=>showToast('Copy failed'));
}

// ── Reconnect banner (B4/B5: reload resilience) ──
const INFLIGHT_KEY = 'hermes-webui-inflight'; // localStorage key for in-flight session tracking
const INFLIGHT_STATE_KEY = 'hermes-webui-inflight-state'; // localStorage snapshots for mid-stream reload recovery

function _readInflightStateMap(){
  try{
    const raw=localStorage.getItem(INFLIGHT_STATE_KEY);
    const parsed=raw?JSON.parse(raw):{};
    return parsed&&typeof parsed==='object'?parsed:{};
  }catch(_){
    return {};
  }
}
function saveInflightState(sid, state){
  if(!sid||!state) return;
  try{
    const all=_readInflightStateMap();
    all[sid]={...state,updated_at:Date.now()};
    localStorage.setItem(INFLIGHT_STATE_KEY, JSON.stringify(all));
  }catch(_){ }
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
    S.messages = data.session.messages || [];
    const pendingMsg=getPendingSessionMessage(data.session);
    if(pendingMsg) S.messages.push(pendingMsg);
    S.activeStreamId=data.session.active_stream_id||null;

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

function getPendingSessionMessage(session){
  const text=String(session?.pending_user_message||'').trim();
  if(!text) return null;
  const attachments=Array.isArray(session?.pending_attachments)?session.pending_attachments.filter(Boolean):[];
  const messages=Array.isArray(session?.messages)?session.messages:[];
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

function syncTopbar(){
  if(!S.session){
    document.title=window._botName||'Hermes';
    if(typeof syncWorkspaceDisplays==='function') syncWorkspaceDisplays();
    if(typeof syncModelChip==='function') syncModelChip();
    if(typeof _syncHermesPanelSessionActions==='function') _syncHermesPanelSessionActions();
    else {
      const sidebarName=$('sidebarWsName');
      if(sidebarName && sidebarName.textContent==='Workspace'){
        sidebarName.textContent=t('no_workspace');
      }
    }
    return;
  }
  const sessionTitle=S.session.title||t('untitled');
  $('topbarTitle').textContent=sessionTitle;
  document.title=sessionTitle+' \u2014 '+(window._botName||'Hermes');
  const vis=S.messages.filter(m=>m&&m.role&&m.role!=='tool');
  $('topbarMeta').textContent=t('n_messages',vis.length);
  // If a profile switch just happened, apply its model rather than the session's stale value.
  // S._pendingProfileModel is set by switchToProfile() and cleared here after one application.
  const modelOverride=S._pendingProfileModel;
  let currentModel=S.session.model||'';
  if(modelOverride){
    S._pendingProfileModel=null;
    _applyModelToDropdown(modelOverride,$('modelSelect'));
    currentModel=modelOverride;
  } else {
    const applied=_applyModelToDropdown(currentModel,$('modelSelect'));
    // If the model isn't in the current provider list, add it as a visually marked
    // "(unavailable)" entry so the session value is preserved without misleading the user.
    // Selecting it will still attempt to send (same as before), but the label makes
    // clear it's a stale model from a previous session.
    if(!applied && currentModel){
      const opt=document.createElement('option');
      opt.value=currentModel;
      opt.textContent=getModelLabel(currentModel)+t('model_unavailable');
      opt.style.color='var(--muted, #888)';
      opt.title=t('model_unavailable_title');
      $('modelSelect').appendChild(opt);
      $('modelSelect').value=currentModel;
    }
  }
  if(typeof syncModelChip==='function') syncModelChip();
  // Show Clear button only when session has messages
  const clearBtn=$('btnClearConv');
  if(clearBtn) clearBtn.style.display=(S.messages&&S.messages.filter(msg=>msg.role!=='tool').length>0)?'':'none';
  if(typeof _syncHermesPanelSessionActions==='function') _syncHermesPanelSessionActions();
  if(typeof syncWorkspaceDisplays==='function') syncWorkspaceDisplays();
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
    // Keep assistant messages with tool_use content even if they have no text,
    // so tool cards can be anchored to their DOM rows on page reload (#140).
    if(m.role==='assistant'&&Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use'))return true;
    return msgContent(m)||m.attachments?.length;
  });
  $('emptyState').style.display=vis.length?'none':'';
  inner.innerHTML='';
  // Track original indices (in S.messages) so truncate knows the cut point.
  // Also include assistant messages that have tool_calls (OpenAI format) or
  // tool_use content (Anthropic format) even when their text is empty — these
  // rows serve as DOM anchors for tool card insertion on page reload.
  const visWithIdx=[];
  let rawIdx=0;
  for(const m of S.messages){
    if(!m||!m.role||m.role==='tool'){rawIdx++;continue;}
    const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
    const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
    if(msgContent(m)||m.attachments?.length||(m.role==='assistant'&&(hasTc||hasTu))) visWithIdx.push({m,rawIdx});
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
    // Also check top-level reasoning field (Hermes format)
    if(!thinkingText && m.reasoning){
      thinkingText=m.reasoning;
    }
    // Parse inline thinking tags from plain text: <think>...</think> (DeepSeek, QwQ, MiniMax, etc.)
    // and Gemma 4 channel tokens: <|channel>thought\n...<channel|>
    // Note: no ^ anchor — some models emit leading whitespace/newlines before <think>.
    if(!thinkingText && typeof content==='string'){
      const thinkMatch=content.match(/<think>([\s\S]*?)<\/think>/);
      if(thinkMatch){
        thinkingText=thinkMatch[1].trim();
        content=content.replace(/<think>[\s\S]*?<\/think>\s*/,'').trimStart();
      }
      if(!thinkingText){
        const gemmaMatch=content.match(/<\|channel>thought\n([\s\S]*?)<channel\|>/);
        if(gemmaMatch){
          thinkingText=gemmaMatch[1].trim();
          content=content.replace(/<\|channel>thought\n[\s\S]*?<channel\|>\s*/,'').trimStart();
        }
      }
    }
    const isUser=m.role==='user';
    const isLastAssistant=!isUser&&vi===visWithIdx.length-1;
    // Render thinking card before the assistant message (collapsed by default)
    if(thinkingText&&!isUser){
      const thinkRow=document.createElement('div');thinkRow.className='msg-row thinking-card-row';
      thinkRow.innerHTML=`<div class="thinking-card"><div class="thinking-card-header" onclick="this.parentElement.classList.toggle('open')"><span class="thinking-card-icon">${li('lightbulb',14)}</span><span class="thinking-card-label">${t('thinking')}</span><span class="thinking-card-toggle">${li('chevron-right',12)}</span></div><div class="thinking-card-body"><pre>${esc(thinkingText)}</pre></div></div>`;
      inner.appendChild(thinkRow);
    }
    const row=document.createElement('div');row.className='msg-row';
    row.dataset.msgIdx=rawIdx;row.dataset.role=m.role||'assistant';
    if(m._live) row.setAttribute('data-live-assistant','1');
    let filesHtml='';
    if(m.attachments&&m.attachments.length)
      filesHtml=`<div class="msg-files">${m.attachments.map(f=>`<div class="msg-file-badge">${li('paperclip',12)} ${esc(f)}</div>`).join('')}</div>`;
    const bodyHtml = isUser ? esc(String(content)).replace(/\n/g,'<br>') : renderMd(String(content));
    // Action buttons for this bubble
    const editBtn  = isUser  ? `<button class="msg-action-btn" title="${t('edit_message')}" onclick="editMessage(this)">${li('pencil',13)}</button>` : '';
    const retryBtn = isLastAssistant ? `<button class="msg-action-btn" title="${t('regenerate')}" onclick="regenerateResponse(this)">${li('rotate-ccw',13)}</button>` : '';
    const tsVal=m._ts||m.timestamp;
    const tsTitle=tsVal?new Date(tsVal*1000).toLocaleString():'';
    const _bn=window._botName||'Hermes';
    row.innerHTML=`<div class="msg-role ${m.role}" ${tsTitle?`title="${esc(tsTitle)}"`:''}><div class="role-icon ${m.role}">${isUser?'Y':esc(_bn.charAt(0).toUpperCase())}</div><span style="font-size:12px">${isUser?t('you'):esc(_bn)}</span>${tsTitle?`<span class="msg-time">${new Date(tsVal*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</span>`:''}<span class="msg-actions">${editBtn}<button class="msg-copy-btn msg-action-btn" title="${t('copy')}" onclick="copyMsg(this)">${li('copy',13)}</button>${retryBtn}</span></div>${filesHtml}<div class="msg-body">${bodyHtml}</div>`;
    row.dataset.rawText = String(content).trim();
    inner.appendChild(row);
  }
  // Insert settled tool call cards (history view only).
  // During live streaming, tool cards are rendered in #liveToolCards by the
  // tool SSE handler and never mixed into the message list until done fires.
  //
  // Fallback: if S.toolCalls is empty (sessions that predate session-level tool
  // tracking, or runs that didn't go through the normal streaming path), build
  // a display list from per-message tool_calls (OpenAI format) stored in each
  // assistant message. This covers the reload case described in issue #140.
  if(!S.busy && (!S.toolCalls||!S.toolCalls.length)){
    const derived=[];
    S.messages.forEach((m,rawIdx)=>{
      if(m.role!=='assistant') return;
      (m.tool_calls||[]).forEach(tc=>{
        if(!tc||typeof tc!=='object') return;
        const fn=tc.function||{};
        const name=fn.name||tc.name||'tool';
        let args={};
        try{ args=JSON.parse(fn.arguments||'{}'); }catch(e){}
        let argsSnap={};
        Object.keys(args).slice(0,4).forEach(k=>{ const v=String(args[k]); argsSnap[k]=v.slice(0,120)+(v.length>120?'...':''); });
        derived.push({name,snippet:'',tid:tc.id||tc.call_id||'',assistant_msg_idx:rawIdx,args:argsSnap,done:true});
      });
    });
    if(derived.length) S.toolCalls=derived;
  }
  if(!S.busy && S.toolCalls && S.toolCalls.length){
    inner.querySelectorAll('.tool-card-row').forEach(el=>el.remove());
    const byAssistant = {};
    for(const tc of S.toolCalls){
      const key = tc.assistant_msg_idx !== undefined ? tc.assistant_msg_idx : -1;
      if(!byAssistant[key]) byAssistant[key] = [];
      byAssistant[key].push(tc);
    }
    const allRows = Array.from(inner.querySelectorAll('.msg-row[data-msg-idx]'));
    // Track the last inserted node per anchor so back-to-back groups for the
    // same (filtered) anchor row are inserted in chronological order.
    const anchorInsertAfter = new Map();
    for(const [key, cards] of Object.entries(byAssistant)){
      const aIdx = parseInt(key);
      // Find the right insertion point: cards go AFTER the assistant message
      // that triggered them. We look for the row at aIdx, or the nearest
      // visible ASSISTANT row at or before aIdx (the assistant message may be
      // filtered out if it contained only tool_use blocks with no text response).
      let anchorRow = null;
      if(aIdx >= 0){
        // First: exact match for the assistant row
        for(const r of allRows){
          const ri=parseInt(r.dataset.msgIdx||'-1');
          if(ri===aIdx){anchorRow=r;break;}
        }
        // Fallback: nearest visible ASSISTANT row at or before aIdx
        if(!anchorRow){
          for(let i=allRows.length-1;i>=0;i--){
            const ri=parseInt(allRows[i].dataset.msgIdx||'-1');
            if(ri<=aIdx&&S.messages[ri]&&S.messages[ri].role==='assistant'){anchorRow=allRows[i];break;}
          }
        }
      }
      // aIdx === -1 or no assistant anchor found: attach after the last assistant row
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
      // Insert after the anchor row (or after any previously inserted group for
      // the same anchor), preserving chronological order for multi-step chains.
      const insertAfterNode = anchorInsertAfter.get(anchorRow) || anchorRow;
      const refNode = insertAfterNode ? insertAfterNode.nextSibling : null;
      if(refNode) inner.insertBefore(frag,refNode);
      else inner.appendChild(frag);
      // Record the last child we inserted so the next group for this anchor
      // goes after it rather than back at anchorRow.nextSibling.
      anchorInsertAfter.set(anchorRow, inner.lastChild);
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
  requestAnimationFrame(()=>{highlightCode();addCopyButtons();renderMermaidBlocks();renderKatexBlocks();});
  // Refresh todo panel if it's currently open
  if(typeof loadTodos==='function' && document.getElementById('panelTodos') && document.getElementById('panelTodos').classList.contains('active')){
    loadTodos();
  }
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
  // Clean up legacy subagent prefixes since the Lucide icon already shows it
  let displayName=tc.name;
  if(isSubagent) displayName='Subagent';
  if(isDelegation) displayName='Delegate task';
  let previewText=tc.preview||displaySnippet||'';
  if(isSubagent) previewText=previewText.replace(/^(?:\u{1F500}|↳)\s*/u,'');
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
  } catch(e) { setStatus(t('edit_failed') + e.message); }
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
  } catch(e) { setStatus(t('regen_failed') + e.message); }
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
    btn.textContent=t('copy');
    btn.onclick=(e)=>{
      e.stopPropagation();
      navigator.clipboard.writeText(codeEl.textContent).then(()=>{
        btn.textContent=t('copied');
        setTimeout(()=>{btn.textContent=t('copy');},1500);
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

let _katexLoading=false;
let _katexReady=false;

function renderKatexBlocks(){
  const blocks=document.querySelectorAll('.katex-block:not([data-rendered]),.katex-inline:not([data-rendered])');
  if(!blocks.length) return;
  if(!_katexReady){
    if(!_katexLoading){
      _katexLoading=true;
      const script=document.createElement('script');
      script.src='https://cdn.jsdelivr.net/npm/katex@0.16.22/dist/katex.min.js';
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
    el.dataset.rendered='true';
    const src=el.textContent||'';
    const displayMode=el.dataset.katex==='display';
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
  const _bn=window._botName||'Hermes';
  const icon=esc(_bn.charAt(0).toUpperCase());
  const label=esc(_bn);
  const body=(text&&String(text).trim())
    ? `<div class="thinking-card open"><div class="thinking-card-header"><span class="thinking-card-icon">${li('lightbulb',14)}</span><span class="thinking-card-label">${t('thinking')}</span></div><div class="thinking-card-body"><pre>${esc(String(text).trim())}</pre></div></div>`
    : `<div class="thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>`;
  return `<div class="msg-role assistant"><div class="role-icon assistant">${icon}</div>${label}</div>${body}`;
}
function appendThinking(text=''){
  $('emptyState').style.display='none';
  let row=$('thinkingRow');
  if(!row){
    row=document.createElement('div');
    row.className='msg-row';
    row.id='thinkingRow';
    $('msgInner').appendChild(row);
  }
  row.className=(text&&String(text).trim())?'msg-row thinking-card-row':'msg-row';
  row.innerHTML=_thinkingMarkup(text);
  scrollToBottom();
}
function updateThinking(text=''){appendThinking(text);}
function removeThinking(){const el=$('thinkingRow');if(el)el.remove();}

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
    iconEl.className='file-icon';iconEl.innerHTML=fileIcon(item.name,item.type);
    el.appendChild(iconEl);

    // Name
    const nameEl=document.createElement('span');
    nameEl.className='file-name';nameEl.textContent=item.name;nameEl.title=t('double_click_rename');
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
              showToast(t('renamed_to')+newName);
              // Invalidate cache and re-render
              delete S._dirCache[S.currentDir];
              await loadDir(S.currentDir);
            }catch(err){showToast(t('rename_failed')+err.message);}
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
      del.className='file-del-btn';del.title=t('delete_title');del.textContent='\u00d7';
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
        empty.textContent=t('empty_dir');
        container.appendChild(empty);
      }
    }
  }
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
  if(!S.session)return;
  const name=await showPromptDialog({title:t('new_folder_prompt'),placeholder:'folder-name',confirmLabel:t('create')});
  if(!name||!name.trim())return;
  const relPath=S.currentDir==='.'?name.trim():(S.currentDir+'/'+name.trim());
  try{
    await api('/api/file/create-dir',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath})});
    showToast(t('folder_created')+name.trim());
    await loadDir(S.currentDir);
  }catch(e){setStatus(t('folder_create_failed')+e.message);}
}

function renderTray(){
  const tray=$('attachTray');tray.innerHTML='';
  if(!S.pendingFiles.length){tray.classList.remove('has-files');updateSendBtn();return;}
  tray.classList.add('has-files');
  updateSendBtn();
  S.pendingFiles.forEach((f,i)=>{
    const chip=document.createElement('div');chip.className='attach-chip';
    chip.innerHTML=`${li('paperclip',12)} ${esc(f.name)} <button title="${t('remove_title')}">${li('x',12)}</button>`;
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
    }catch(e){failures++;setStatus(`\u274c ${t('upload_failed')}${f.name} \u2014 ${e.message}`);}
    bar.style.width=`${Math.round((i+1)/total*100)}%`;
  }
  barWrap.classList.remove('active');bar.style.width='0%';
  S.pendingFiles=[];renderTray();
  if(failures===total&&total>0)throw new Error(t('all_uploads_failed',total));
  return names;
}

