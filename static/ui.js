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
    const data=await fetch(new URL('api/models',location.href).href,{credentials:'include'}).then(r=>r.json());
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
    const url=new URL('api/models/live',location.href);
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
  // Don't show a model label until boot has finished loading to prevent flash of wrong default
  if(!S._bootReady){ label.textContent=''; chip.title='Conversation model'; return; }
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
  // Store model data for filtering
  const _modelData=[];
  for(const child of Array.from(sel.children)){
    if(child.tagName==='OPTGROUP'){
      for(const opt of Array.from(child.children)){
        _modelData.push({value:opt.value,name:esc(opt.textContent||getModelLabel(opt.value)),id:esc(opt.value),group:child.label||''});
      }
    }
    if(child.tagName==='OPTION'){
      _modelData.push({value:child.value,name:esc(child.textContent||getModelLabel(child.value)),id:esc(child.value),group:''});
    }
  }
  // Create search input FIRST before filterModels definition
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
    // Clear and rebuild
    dd.innerHTML='';
    // Add search and custom elements first (CRITICAL: must be before models)
    dd.appendChild(_searchRow);
    dd.appendChild(_custSep);
    dd.appendChild(_custRow);
    // Add models matching filter
    let _lastGroup=null;
    for(const m of _modelData){
      if(!term||found.has(m.value)){
        if(m.group&&m.group!==_lastGroup){
          const heading=document.createElement('div');
          heading.className='model-group';
          heading.textContent=m.group;
          dd.appendChild(heading);
          _lastGroup=m.group;
        }
        const row=document.createElement('div');
        row.className='model-opt'+(m.value===sel.value?' active':'');
        row.innerHTML=`<span class="model-opt-name">${m.name}</span><span class="model-opt-id">${m.id}</span>`;
        row.onclick=()=>selectModelFromDropdown(m.value);
        dd.appendChild(row);
      }
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
  dd.appendChild(_searchRow);
  dd.appendChild(_custSep);
  dd.appendChild(_custRow);
  // Apply initial filter (empty shows all)
  _filterModels('');
}

async function selectModelFromDropdown(value){
  const sel=$('modelSelect');
  if(!sel||sel.value===value) { closeModelDropdown(); return; }
  // If the value isn't in the option list (custom model ID), add a temporary option
  // so sel.value assignment succeeds and the model chip shows the custom ID.
  if(!Array.from(sel.options).some(o=>o.value===value)){
    const opt=document.createElement('option');
    opt.value=value;
    opt.textContent=value.split('/').pop()||value;
    opt.dataset.custom='1';
    // Remove any previous custom option before adding new one
    sel.querySelectorAll('option[data-custom]').forEach(o=>o.remove());
    sel.appendChild(opt);
  }
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
// Once the user scrolls back to within 150px of the bottom, re-pin.
let _scrollPinned=true;
(function(){
  const el=document.getElementById('messages');
  if(!el) return;
  el.addEventListener('scroll',()=>{
    const nearBottom=el.scrollHeight-el.scrollTop-el.clientHeight<150;
    _scrollPinned=nearBottom;
    const btn=$('scrollToBottomBtn');
    if(btn) btn.style.display=_scrollPinned?'none':'flex';
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
  const btn=$('scrollToBottomBtn');
  if(btn) btn.style.display='none';
}

function getModelLabel(modelId){
  if(!modelId) return 'Unknown';
  // Check dynamic labels first, then fall back to splitting the ID
  if(_dynamicModelLabels[modelId]) return _dynamicModelLabels[modelId];
  // Static fallback for common models
  const STATIC_LABELS={'openai/gpt-5.4-mini':'GPT-5.4 Mini','openai/gpt-4o':'GPT-4o','openai/o3':'o3','openai/o4-mini':'o4-mini','anthropic/claude-sonnet-4.6':'Sonnet 4.6','anthropic/claude-sonnet-4-5':'Sonnet 4.5','anthropic/claude-haiku-3-5':'Haiku 3.5','google/gemini-3.1-pro-preview':'Gemini 3.1 Pro','google/gemini-3-flash-preview':'Gemini 3 Flash','google/gemini-3.1-flash-lite-preview':'Gemini 3.1 Flash Lite','google/gemini-2.5-pro':'Gemini 2.5 Pro','google/gemini-2.5-flash':'Gemini 2.5 Flash','deepseek/deepseek-chat-v3-0324':'DeepSeek V3','meta-llama/llama-4-scout':'Llama 4 Scout'};
  if(STATIC_LABELS[modelId]) return STATIC_LABELS[modelId];
  return modelId.split('/').pop()||'Unknown';
}

function _stripXmlToolCallsDisplay(s){
  // Strip <function_calls>...</function_calls> blocks emitted by DeepSeek and
  // similar models in their raw response text.  These are processed separately
  // as tool calls; leaving them in the content causes them to render visibly
  // in the settled chat bubble.  (#702)
  if(!s||s.toLowerCase().indexOf('<function_calls>')===-1) return s;
  s=s.replace(/<function_calls>[\s\S]*?<\/function_calls>/gi,'');
  s=s.replace(/<function_calls>[\s\S]*$/i,'');
  return s.trim();
}

function renderMd(raw){
  let s=raw||'';
  // ── MEDIA: token stash (must run first, before any other processing) ───────
  // Detect MEDIA:<path-or-url> tokens emitted by the agent (e.g. screenshots,
  // generated images) and replace them with inline <img> or download links.
  // Stashed so the path/URL is never processed as markdown.
  const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico)$/i;
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
  s=s.replace(/```([\w+-]*)\n?([\s\S]*?)```/g,(_,lang,code)=>{
    const normalizedLang=(lang||'').trim().toLowerCase();
    const h=normalizedLang?`<div class="pre-header">${esc(normalizedLang)}</div>`:'';
    const langAttr=normalizedLang?` class="language-${esc(normalizedLang)}"`:'';
    return `${h}<pre><code${langAttr}>${esc(code.replace(/\n$/,''))}</code></pre>`;
  });
  s=s.replace(/`([^`\n]+)`/g,(_,c)=>`<code>${esc(c)}</code>`);
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
    // #487: Image pass — runs while code stash is active so ![x](url) inside
    // backticks stays protected as a \x00C token and is never rendered as <img>.
    // Must run before _code_stash restore and before _link_stash so the image
    // is not consumed by the [label](url) link regex.
    t=t.replace(/!\[([^\]]*)\]\((https?:\/\/[^\)]+)\)/g,(_,alt,url)=>`<img src="${url.replace(/"/g,'%22')}" alt="${esc(alt)}" class="msg-media-img" loading="lazy" onclick="this.classList.toggle('msg-media-img--full')">`);
    // Stash rendered <img> tags so autolink never matches URLs inside src=
    const _img_stash=[];
    t=t.replace(/(<img\b[^>]*>)/g,m=>{_img_stash.push(m);return `\x00G${_img_stash.length-1}\x00`;});
    t=t.replace(/\x00C(\d+)\x00/g,(_,i)=>_code_stash[+i]);
    // Stash [label](url) links before autolink so the URL in href= is not re-linked
    const _link_stash=[];
    t=t.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g,(_,lb,u)=>{_link_stash.push(`<a href="${u.replace(/"/g,'%22')}" target="_blank" rel="noopener">${esc(lb)}</a>`);return `\x00L${_link_stash.length-1}\x00`;});
    t=t.replace(/(https?:\/\/[^\s<>"')\]]+)/g,(url)=>{const trail=url.match(/[.,;:!?)]$/)?url.slice(-1):'';const clean=trail?url.slice(0,-1):url;return `<a href="${clean}" target="_blank" rel="noopener">${esc(clean)}</a>${trail}`;});
    t=t.replace(/\x00L(\d+)\x00/g,(_,i)=>_link_stash[+i]);
    t=t.replace(/\x00G(\d+)\x00/g,(_,i)=>_img_stash[+i]);
    // Escape any plain text that isn't already wrapped in a tag we produced
    // by escaping bare < > that are not part of our own tags
    const SAFE_INLINE=/^<\/?(strong|em|code|a|img)([\s>]|$)/i;
    t=t.replace(/<\/?[a-z][^>]*>/gi,tag=>SAFE_INLINE.test(tag)?tag:esc(tag));
    return t;
  }
  // Stash <code> tags from the backtick pass above so the outer bold/italic
  // regexes don't esc() their content (e.g. **`code`** → <strong><code>code</code></strong>)
  const _ob_stash=[];
  s=s.replace(/(<code>[^<]*<\/code>)/g,m=>{_ob_stash.push(m);return `\x00O${_ob_stash.length-1}\x00`;});
  s=s.replace(/\*\*\*(.+?)\*\*\*/g,(_,t)=>`<strong><em>${esc(t)}</em></strong>`);
  s=s.replace(/\*\*(.+?)\*\*/g,(_,t)=>`<strong>${esc(t)}</strong>`);
  s=s.replace(/\*([^*\n]+)\*/g,(_,t)=>`<em>${esc(t)}</em>`);
  s=s.replace(/\x00O(\d+)\x00/g,(_,i)=>_ob_stash[+i]);
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
  // Tables: | col | col | header row followed by | --- | --- | separator then data rows
  // NOTE: table pass runs BEFORE outer link pass so [label](url) in table cells
  // is handled by inlineMd() only — prevents double-linking.
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
  // #487: Outer image pass — handles ![alt](url) in plain paragraphs (outside tables/lists).
  // Runs AFTER the table pass (images in table cells are handled by inlineMd() above).
  // Runs BEFORE the outer [label](url) link pass so the image is not consumed as a plain link.
  s=s.replace(/!\[([^\]]*)\]\((https?:\/\/[^\)]+)\)/g,(_,alt,url)=>`<img src="${url.replace(/"/g,'%22')}" alt="${esc(alt)}" class="msg-media-img" loading="lazy" onclick="this.classList.toggle('msg-media-img--full')">`);
  // Outer link pass for labeled links in plain paragraphs (outside table cells).
  // Runs AFTER the table pass so table cells are processed by inlineMd() only.
  // Stash existing <a> tags first to avoid re-linking already-linked URLs.
  const _a_stash=[];
  s=s.replace(/(<a\b[^>]*>[\s\S]*?<\/a>)/g,m=>{_a_stash.push(m);return `\x00A${_a_stash.length-1}\x00`;});
  s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g,(_,label,url)=>`<a href="${url.replace(/"/g,'%22')}" target="_blank" rel="noopener">${esc(label)}</a>`);
  s=s.replace(/\x00A(\d+)\x00/g,(_,i)=>_a_stash[+i]);
  // Escape any remaining HTML tags that are NOT from our own markdown output.
  // Our pipeline only emits: <strong>,<em>,<code>,<pre>,<h1-6>,<ul>,<ol>,<li>,
  // <table>,<thead>,<tbody>,<tr>,<th>,<td>,<hr>,<blockquote>,<p>,<br>,<a>,
  // <div class="..."> (mermaid/pre-header). Everything else is untrusted input.
  const SAFE_TAGS=/^<\/?(strong|em|code|pre|h[1-6]|ul|ol|li|table|thead|tbody|tr|th|td|hr|blockquote|p|br|a|img|div|span)([\s>]|$)/i;
  s=s.replace(/<\/?[a-z][^>]*>/gi,tag=>SAFE_TAGS.test(tag)?tag:esc(tag));
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
  // Stash rendered <pre> blocks (with optional pre-header div) and mermaid/katex
  // divs before paragraph splitting so \n inside code blocks is never replaced
  // with <br>. Token \x00E (next free after B D F G L M C O A).
  // Fixes #745: code blocks collapse to single line when not preceded by blank line.
  const _pre_stash=[];
  s=s.replace(/(<div class="pre-header">[\s\S]*?<\/div>)?<pre>[\s\S]*?<\/pre>|<div class="(mermaid-block|katex-block)"[\s\S]*?<\/div>/g,m=>{
    _pre_stash.push(m);
    return '\x00E'+(_pre_stash.length-1)+'\x00';
  });
  const parts=s.split(/\n{2,}/);
  s=parts.map(p=>{p=p.trim();if(!p)return '';if(/^<(h[1-6]|ul|ol|pre|hr|blockquote)|^\x00E/.test(p))return p;return `<p>${p.replace(/\n/g,'<br>')}</p>`;}).join('\n');
  s=s.replace(/\x00E(\d+)\x00/g,(_,i)=>_pre_stash[+i]);
  // ── Restore MEDIA stash → inline images or download links ─────────────────
  s=s.replace(/\x00D(\d+)\x00/g,(_,i)=>{
    const ref=media_stash[+i];
    // HTTP(S) URL
    if(/^https?:\/\//i.test(ref)){
      // Rewrite localhost/127.0.0.1 to the actual server base URL so remote
      // users (VPN, Docker, deployed) can load agent-generated images (#642).
      // Strip the trailing slash from document.baseURI so the URL's own path
      // joins cleanly — this preserves any subpath mount (e.g. /hermes/).
      let src=ref;
      if(/^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?/i.test(src)){
        const base=document.baseURI.replace(/\/$/,'');
        src=src.replace(/^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?/i,base);
      }
      if(_IMAGE_EXTS.test(src.split('?')[0])){
        return `<img class="msg-media-img" src="${esc(src)}" alt="image" loading="lazy" onclick="this.classList.toggle('msg-media-img--full')">`;
      }
      return `<a href="${esc(src)}" target="_blank" rel="noopener">${esc(src)}</a>`;
    }
    // Local file path
    const apiUrl='api/media?path='+encodeURIComponent(ref);
    if(_IMAGE_EXTS.test(ref)){
      return `<img class="msg-media-img" src="${esc(apiUrl)}" alt="${esc(ref.split('/').pop())}" loading="lazy" onclick="this.classList.toggle('msg-media-img--full')">`;
    }
    // Non-image local file — show download link with filename
    const fname=esc(ref.split('/').pop()||ref);
    return `<a class="msg-media-link" href="${esc(apiUrl+'&download=1')}" download="${fname}">📎 ${fname}</a>`;
  });
  // ── End MEDIA restore ──────────────────────────────────────────────────────
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

let _composerLockState=null;

function lockComposerForClarify(placeholderText){
  const input=$('msg');
  if(!input) return;
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

function updateSendBtn(){
  const btn=$('btnSend');
  if(!btn) return;
  const msg=$('msg');
  const hasContent=msg&&msg.value.trim().length>0||S.pendingFiles.length>0;
  const canSend=hasContent&&!S.busy&&!(msg&&msg.disabled);
  // Hide while busy (cancel button takes its place); show otherwise
  btn.style.display=S.busy?'none':'';
  btn.disabled=!canSend;
  if(canSend&&!btn.classList.contains('visible')){
    btn.classList.remove('visible');
    requestAnimationFrame(()=>btn.classList.add('visible'));
  } else if(!canSend){
    btn.classList.remove('visible');
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
      if(e.isComposing) return;
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
  const row=btn.closest('[data-raw-text]');
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
  return /<think>[\s\S]*?<\/think>|<\|channel>thought\n[\s\S]*?<channel\|>|<\|turn\|>thinking\n[\s\S]*?<turn\|>/.test(String(m.content||''));
}
function _assistantRoleHtml(tsTitle=''){
  const _bn=window._botName||'Hermes';
  return `<div class="msg-role assistant" ${tsTitle?`title="${esc(tsTitle)}"`:''}><div class="role-icon assistant">${esc(_bn.charAt(0).toUpperCase())}</div><span style="font-size:12px">${esc(_bn)}</span></div>`;
}
function _createAssistantTurn(tsTitle=''){
  const row=document.createElement('div');
  row.className='msg-row assistant-turn';
  row.dataset.role='assistant';
  row.innerHTML=`${_assistantRoleHtml(tsTitle)}<div class="assistant-turn-blocks"></div>`;
  return row;
}
function _assistantTurnBlocks(turn){
  return turn?turn.querySelector('.assistant-turn-blocks'):null;
}
function _thinkingCardHtml(text){
  return `<div class="thinking-card"><div class="thinking-card-header" onclick="this.parentElement.classList.toggle('open')"><span class="thinking-card-icon">${li('lightbulb',14)}</span><span class="thinking-card-label">${t('thinking')}</span><span class="thinking-card-toggle">${li('chevron-right',12)}</span></div><div class="thinking-card-body"><pre>${esc(text)}</pre></div></div>`;
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
function clearCompressionUi(){
  window._compressionUi=null;
  _setCompressionSessionLock(null);
  renderCompressionUi();
}
function setCompressionUi(state){
  if(!state){
    clearCompressionUi();
    return;
  }
  window._compressionUi={...state};
  if(state.sessionId) _setCompressionSessionLock(state.sessionId);
  renderCompressionUi();
}
function _compressionCardsHtml(state){
  if(!state) return '';
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
function _compressionCardsNode(state){
  const wrap=document.createElement('div');
  wrap.className='compression-turn';
  wrap.innerHTML=`<div class="compression-turn-blocks">${_compressionCardsHtml(state)}</div>`;
  return wrap;
}
function _isContextCompactionMessage(m){
  if(!m||m.role!=='assistant') return false;
  const text=msgContent(m)||String(m.content||'');
  return /^\s*\[context compaction/i.test(text) || /^\s*context compaction/i.test(text);
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
      if(
        candidate.role===String(anchorKey.role||'') &&
        String(candidate.ts??'')===String(anchorKey.ts??'') &&
        String(candidate.text||'')===String(anchorKey.text||'') &&
        Number(candidate.attachments||0)===Number(anchorKey.attachments||0)
      ){
        return i;
      }
    }
  }
  return typeof fallbackIdx==='number' ? fallbackIdx : null;
}
function _compressionReferenceCardHtml(text, open=false){
  const preview=text.split(/\n+/).filter(Boolean).slice(0,2).join(' ');
  return `
    <div class="tool-card-row compression-card-row" data-compression-card="1" data-raw-text="${esc(text)}">
      <div class="tool-card tool-card-compress-reference${open?' open':''}">
        <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
          <span class="tool-card-icon">${li('star',13)}</span>
          <span class="tool-card-name">${esc(t('context_compaction_label'))}</span>
          <span class="tool-card-preview">${esc(t('reference_only_label'))} · ${esc(preview)}</span>
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
function _isSameLocalDay(dateA, dateB){
  return dateA.getFullYear()===dateB.getFullYear()
    && dateA.getMonth()===dateB.getMonth()
    && dateA.getDate()===dateB.getDate();
}
function _formatMessageFooterTimestamp(tsVal){
  if(!tsVal) return '';
  const date=new Date(tsVal*1000);
  const now=new Date();
  if(_isSameLocalDay(date, now)){
    return date.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  }
  return date.toLocaleString([], {
    month:'short',
    day:'numeric',
    hour:'numeric',
    minute:'2-digit',
  });
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
function _contextCompactionMessageHtml(m, tsTitle=''){
  const text=msgContent(m)||String(m.content||'');
  return `<div class="compression-turn"><div class="compression-turn-blocks">${_compressionReferenceCardHtml(text, false, tsTitle)}</div></div>`;
}
function renderCompressionUi(){
  const el=$('liveCompressionCards');
  if(!el) return;
  el.innerHTML='';
  el.style.display='none';
}
function renderMessages(){
  const inner=$('msgInner');
  const compressionState=_compressionStateForCurrentSession();
  if(window._compressionUi && !compressionState) clearCompressionUi();
  const sessionCompressionAnchor=(
    S.session && typeof S.session.compression_anchor_visible_idx==='number'
  ) ? S.session.compression_anchor_visible_idx : null;
  const sessionCompressionAnchorKey=(
    S.session && S.session.compression_anchor_message_key && typeof S.session.compression_anchor_message_key==='object'
  ) ? S.session.compression_anchor_message_key : null;
  const vis=S.messages.filter(m=>{
    if(!m||!m.role||m.role==='tool')return false;
    if(m.role==='assistant'){
      const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
      const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
      if(hasTc||hasTu||_messageHasReasoningPayload(m)) return true;
    }
    return msgContent(m)||m.attachments?.length;
  });
  $('emptyState').style.display=vis.length?'none':'';
  inner.innerHTML='';
  const compressionNode=compressionState?_compressionCardsNode(compressionState):null;
  const referenceMessage=S.messages.find(m=>_isContextCompactionMessage(m));
  const referenceText=referenceMessage?msgContent(referenceMessage)||String(referenceMessage.content||''):'';
  const referenceNode=(!compressionState && referenceMessage && (sessionCompressionAnchor!==null || sessionCompressionAnchorKey))
    ? (()=>{const row=document.createElement('div');row.innerHTML=_compressionReferenceCardHtml(referenceText,false);return row.firstElementChild;})()
    : null;
  const visWithIdx=[];
  let rawIdx=0;
  for(const m of S.messages){
    if(!m||!m.role||m.role==='tool'){rawIdx++;continue;}
    const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
    const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
    if(msgContent(m)||m.attachments?.length||(m.role==='assistant'&&(hasTc||hasTu||_messageHasReasoningPayload(m)))) visWithIdx.push({m,rawIdx});
    rawIdx++;
  }
  let lastUserRawIdx=-1;
  for(let i=visWithIdx.length-1;i>=0;i--){
    if(visWithIdx[i].m&&visWithIdx[i].m.role==='user'){
      lastUserRawIdx=visWithIdx[i].rawIdx;
      break;
    }
  }
  const insertionAnchor=_compressionAnchorIndex(
    visWithIdx,
    compressionState ? compressionState.anchorMessageKey : sessionCompressionAnchorKey,
    compressionState
      ? (typeof compressionState.anchorVisibleIdx==='number' ? compressionState.anchorVisibleIdx : compressionState.anchorRawIdx)
      : sessionCompressionAnchor
  );
  let _prevSepKey=null;
  let currentAssistantTurn=null;
  const assistantSegments=new Map();
  const userRows=new Map();
  for(let vi=0;vi<visWithIdx.length;vi++){
    const {m,rawIdx}=visWithIdx[vi];
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
    if(!thinkingText && m.reasoning) thinkingText=m.reasoning;
    if(!thinkingText && typeof content==='string'){
      const thinkMatch=content.match(/<think>([\s\S]*?)<\/think>/);
      if(thinkMatch){
        thinkingText=thinkMatch[1].trim();
        content=content.replace(/<think>[\s\S]*?<\/think>\s*/,'').trimStart();
      }
      if(!thinkingText){
        // Historical name "gemmaMatch" refers to MiniMax <|channel>thought format.
        const gemmaMatch=content.match(/<\|channel>thought\n([\s\S]*?)<channel\|>/);
        if(gemmaMatch){
          thinkingText=gemmaMatch[1].trim();
          content=content.replace(/<\|channel>thought\n[\s\S]*?<channel\|>\s*/,'').trimStart();
        }
      }
      if(!thinkingText){
        // Gemma 4 uses asymmetric <|turn|>thinking\n...<turn|> delimiters.
        const gemmaTurnMatch=content.match(/<\|turn\|>thinking\n([\s\S]*?)<turn\|>/);
        if(gemmaTurnMatch){
          thinkingText=gemmaTurnMatch[1].trim();
          content=content.replace(/<\|turn\|>thinking\n[\s\S]*?<turn\|>\s*/,'').trimStart();
        }
      }
    }
    const isUser=m.role==='user';
    const isLastAssistant=!isUser&&vi===visWithIdx.length-1;
    let filesHtml='';
    if(m.attachments&&m.attachments.length){
      filesHtml=`<div class="msg-files">${m.attachments.map(f=>`<div class="msg-file-badge">${li('paperclip',12)} ${esc(f)}</div>`).join('')}</div>`;
    }
    const bodyHtml = isUser ? esc(String(content)).replace(/\n/g,'<br>') : renderMd(_stripXmlToolCallsDisplay(String(content)));
    const isEditableUser=isUser&&rawIdx===lastUserRawIdx;
    const editBtn  = isEditableUser ? `<button class="msg-action-btn" title="${t('edit_message')}" onclick="editMessage(this)">${li('pencil',13)}</button>` : '';
    const retryBtn = isLastAssistant ? `<button class="msg-action-btn" title="${t('regenerate')}" onclick="regenerateResponse(this)">${li('rotate-ccw',13)}</button>` : '';
    const copyBtn  = `<button class="msg-copy-btn msg-action-btn" title="${t('copy')}" onclick="copyMsg(this)">${li('copy',13)}</button>`;
    const tsVal=m._ts||m.timestamp;
    const tsTitle=tsVal?new Date(tsVal*1000).toLocaleString():'';
    const tsTime=_formatMessageFooterTimestamp(tsVal);
    const timeHtml = tsTime ? `<span class="msg-time" title="${esc(tsTitle)}">${tsTime}</span>` : '';
    const footHtml = `<div class="msg-foot">${timeHtml}<span class="msg-actions">${editBtn}${copyBtn}${retryBtn}</span></div>`;

    if(isUser){
      currentAssistantTurn=null;
      const row=document.createElement('div');
      row.className='msg-row';
      row.dataset.msgIdx=rawIdx;
      row.dataset.role='user';
      row.dataset.rawText=String(content).trim();
      row.innerHTML=`${filesHtml}<div class="msg-body">${bodyHtml}</div>${footHtml}`;
      inner.appendChild(row);
      userRows.set(rawIdx, row);
      continue;
    }

    if(_isContextCompactionMessage(m)){
      if(compressionState || referenceNode){
        continue;
      }else{
        currentAssistantTurn=null;
        const row=document.createElement('div');
        row.innerHTML=_contextCompactionMessageHtml(m, tsTitle);
        inner.appendChild(row.firstElementChild);
        continue;
      }
    }

    if(!currentAssistantTurn){
      currentAssistantTurn=_createAssistantTurn(tsTitle);
      inner.appendChild(currentAssistantTurn);
    }
    const seg=document.createElement('div');
    seg.className='assistant-segment';
    seg.dataset.msgIdx=rawIdx;
    seg.dataset.rawText=String(content).trim();
    if(m._live){
      currentAssistantTurn.id='liveAssistantTurn';
      seg.setAttribute('data-live-assistant','1');
    }
    if(_ERR_MSG_RE.test(String(content||'').trim())) seg.dataset.error='1';
    if(thinkingText) seg.insertAdjacentHTML('beforeend', _thinkingCardHtml(thinkingText));
    const hasVisibleBody=!!(String(content||'').trim()||filesHtml);
    if(hasVisibleBody){
      seg.insertAdjacentHTML('beforeend', `${filesHtml}<div class="msg-body">${bodyHtml}</div>${footHtml}`);
    }else if(!thinkingText){
      seg.classList.add('assistant-segment-anchor');
    }
    _assistantTurnBlocks(currentAssistantTurn).appendChild(seg);
    assistantSegments.set(rawIdx, seg);
  }

  function _insertCompressionLikeNode(node){
    if(!node) return;
    if(insertionAnchor!==null && visWithIdx[insertionAnchor]){
      const anchorRawIdx=visWithIdx[insertionAnchor].rawIdx;
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

  _insertCompressionLikeNode(compressionNode);
  _insertCompressionLikeNode(referenceNode);
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
    // Pass 1: index tool outputs by tool_call_id / tool_use_id so the
    // fallback-built cards carry their result snippet (not just the command).
    // Without this step CLI-origin sessions reload with empty tool cards.
    const resultsByTid={};
    const _snipFromRaw=(raw)=>{
      const s=String(raw||'');
      try{
        const rd=JSON.parse(s);
        if(rd && typeof rd==='object') return String(rd.output||rd.result||rd.error||s).slice(0,200);
      }catch(e){}
      return s.slice(0,200);
    };
    S.messages.forEach(m=>{
      if(!m) return;
      // OpenAI / Hermes CLI format: role=tool with tool_call_id
      if(m.role==='tool'){
        const tid=m.tool_call_id||m.tool_use_id||'';
        if(tid) resultsByTid[tid]=_snipFromRaw(m.content);
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
          resultsByTid[tid]=_snipFromRaw(raw);
        });
      }
    });
    const derived=[];
    S.messages.forEach((m,rawIdx)=>{
      if(m.role!=='assistant') return;
      // OpenAI format: top-level tool_calls field on the assistant message
      (m.tool_calls||[]).forEach(tc=>{
        if(!tc||typeof tc!=='object') return;
        const fn=tc.function||{};
        const name=fn.name||tc.name||'tool';
        let args={};
        try{ args=JSON.parse(fn.arguments||'{}'); }catch(e){}
        let argsSnap={};
        Object.keys(args).slice(0,4).forEach(k=>{ const v=String(args[k]); argsSnap[k]=v.slice(0,120)+(v.length>120?'...':''); });
        const tid=tc.id||tc.call_id||'';
        derived.push({name,snippet:resultsByTid[tid]||'',tid,assistant_msg_idx:rawIdx,args:argsSnap,done:true});
      });
      // Anthropic format: tool_use blocks inside assistant content array
      if(Array.isArray(m.content)){
        m.content.forEach(p=>{
          if(!p||typeof p!=='object'||p.type!=='tool_use') return;
          const name=p.name||'tool';
          const args=p.input||{};
          const argsSnap={};
          if(args && typeof args==='object'){
            Object.keys(args).slice(0,4).forEach(k=>{ const v=String(args[k]); argsSnap[k]=v.slice(0,120)+(v.length>120?'...':''); });
          }
          const tid=p.id||'';
          derived.push({name,snippet:resultsByTid[tid]||'',tid,assistant_msg_idx:rawIdx,args:argsSnap,done:true});
        });
      }
    });
    if(derived.length) S.toolCalls=derived;
  }
  if(!S.busy && S.toolCalls && S.toolCalls.length){
    inner.querySelectorAll('.tool-card-row:not([data-compression-card])').forEach(el=>el.remove());
    const byAssistant = {};
    for(const tc of S.toolCalls){
      const key = tc.assistant_msg_idx !== undefined ? tc.assistant_msg_idx : -1;
      if(!byAssistant[key]) byAssistant[key] = [];
      byAssistant[key].push(tc);
    }
    const assistantIdxs=[...assistantSegments.keys()].sort((a,b)=>a-b);
    const anchorInsertAfter = new Map();
    for(const [key, cards] of Object.entries(byAssistant)){
      const aIdx = parseInt(key);
      let anchorRow=assistantSegments.get(aIdx)||null;
      if(!anchorRow&&assistantIdxs.length){
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
  // Render cumulative usage on the last assistant footer row (if enabled).
  if(window._showTokenUsage&&S.session&&(S.session.input_tokens||S.session.output_tokens)){
    const rows=inner.querySelectorAll('.assistant-turn');
    let lastAssist=null;
    for(let i=rows.length-1;i>=0;i--){lastAssist=rows[i];break;}
    if(lastAssist){
      const footerRows=lastAssist.querySelectorAll('.msg-foot');
      const targetFoot=footerRows.length?footerRows[footerRows.length-1]:null;
      if(targetFoot&&!targetFoot.querySelector('.msg-usage-inline')){
        const usage=document.createElement('span');
        usage.className='msg-usage-inline';
        const inTok=S.session.input_tokens||0;
        const outTok=S.session.output_tokens||0;
        const cost=S.session.estimated_cost;
        let text=`${_fmtTokens(inTok)} in · ${_fmtTokens(outTok)} out`;
        if(cost) text+=` · ~$${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
        usage.textContent=text;
        targetFoot.classList.add('msg-foot-with-usage');
        targetFoot.insertBefore(usage, targetFoot.firstChild);
      }
    }
  }
  // Only force-scroll when not actively streaming — mid-stream re-renders
  // (tool completion, session switch) must not override the user's scroll position.
  // scrollIfPinned() respects _scrollPinned, so it's a no-op if user scrolled up.
  if(S.activeStreamId){
    scrollIfPinned();
  } else {
    scrollToBottom();
  }
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
  row.className='tool-card-row';
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
        ${hasDetail?`<span class="tool-card-toggle">${li('chevron-right',12)}</span>`:''}
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
// Live cards are inserted INLINE inside #msgInner (tagged with data-live-tid)
// so the streaming layout matches the settled layout produced by renderMessages
// (user → thinking → tool cards → response). The legacy #liveToolCards
// sibling container is no longer used for placement — keeping the cards in the
// message column eliminates the visible "jump" users saw when renderMessages
// fired on the done event.
function appendLiveToolCard(tc){
  let turn=$('liveAssistantTurn');
  if(!turn){
    appendThinking();
    turn=$('liveAssistantTurn');
  }
  const inner=_assistantTurnBlocks(turn);
  if(!inner) return;
  const tid=tc.tid||'';
  // Update existing card in place (tool_complete after tool_start)
  if(tid){
    const existing=inner.querySelector(`.tool-card-row[data-live-tid="${CSS.escape(tid)}"]`);
    if(existing){
      const replacement=buildToolCard(tc);
      replacement.dataset.liveTid=tid;
      existing.replaceWith(replacement);
      return;
    }
  }
  const row=buildToolCard(tc);
  if(tid) row.dataset.liveTid=tid;
  // Insert BEFORE the live assistant segment if it exists, so tool cards stay
  // between the current thinking block(s) and the streaming response.
  const liveAssistant=inner.querySelector('[data-live-assistant="1"]');
  if(liveAssistant) inner.insertBefore(row, liveAssistant);
  else inner.appendChild(row);
  if(typeof scrollIfPinned==='function') scrollIfPinned();
}

function clearLiveToolCards(){
  const inner=_assistantTurnBlocks($('liveAssistantTurn'));
  if(inner) inner.querySelectorAll('.tool-card-row[data-live-tid]').forEach(el=>el.remove());
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
    if(e.key==='Enter' && !e.shiftKey) { if(e.isComposing) return; e.preventDefault(); bar.querySelector('.msg-edit-send').click(); }
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
          mermaid.initialize({startOnLoad:false,theme:document.documentElement.classList.contains('dark')?'dark':'default',themeVariables:{
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
  return (text&&String(text).trim())
    ? `<div class="thinking-card open"><div class="thinking-card-header" onclick="this.parentElement.classList.toggle('open')"><span class="thinking-card-icon">${li('lightbulb',14)}</span><span class="thinking-card-label">${t('thinking')}</span><span class="thinking-card-toggle">${li('chevron-right',12)}</span></div><div class="thinking-card-body"><pre>${esc(String(text).trim())}</pre></div></div>`
    : `<div class="thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>`;
}
function finalizeThinkingCard(){
  const row=$('thinkingRow');
  if(!row) return;
  row.removeAttribute('id');
  row.removeAttribute('data-thinking-active');
}
function appendThinking(text=''){
  $('emptyState').style.display='none';
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    $('msgInner').appendChild(turn);
  }
  const blocks=_assistantTurnBlocks(turn);
  let row=$('thinkingRow');
  if(!row){
    row=document.createElement('div');
    row.className='assistant-segment';
    row.id='thinkingRow';
    row.setAttribute('data-thinking-active','1');
    blocks.appendChild(row);
  }
  row.className=(text&&String(text).trim())?'assistant-segment thinking-card-row':'assistant-segment';
  row.innerHTML=_thinkingMarkup(text);
  scrollIfPinned();
}
function updateThinking(text=''){appendThinking(text);}
function removeThinking(){
  const el=$('thinkingRow');
  if(el) el.remove();
  const turn=$('liveAssistantTurn');
  const blocks=_assistantTurnBlocks(turn);
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
  if(!S.entries||!S.entries.length){
    if(emptyEl){emptyEl.textContent=t('workspace_empty_dir');emptyEl.style.display='flex';}
    return;
  }
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
        if(e2.key==='Enter'){
          if(e2.isComposing){return;}
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
      const res=await fetch(new URL('api/upload',location.href).href,{method:'POST',credentials:'include',body:fd});
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
