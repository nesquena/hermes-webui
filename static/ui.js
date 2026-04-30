const S={session:null,messages:[],entries:[],busy:false,pendingFiles:[],toolCalls:[],activeStreamId:null,currentDir:'.',activeProfile:'default'};
const INFLIGHT={};  // keyed by session_id while request in-flight
const SESSION_QUEUES={};  // keyed by session_id for queued follow-up turns
// Tracks which session's queue to drain in setBusy(false).
// Set to activeSid just before setBusy(false) in done/error handlers so the
// queue drains the session that *finished*, not the one currently viewed.
// Single-shot: setBusy() reads and clears this on every call. Concurrent
// back-to-back stream completions would overwrite it, but HTTPServer is
// single-threaded so only one done event fires at a time in practice.
let _queueDrainSid=null;
const $=id=>document.getElementById(id);
// Redirect to /login when the server responds with 401 (auth session expired).
// Handles iOS PWA standalone mode where a server-side 302→/login would break
// out of the PWA shell into Safari instead of navigating within it.
function _redirectIfUnauth(res){if(res&&res.status===401){window.location.href='/login?next='+encodeURIComponent(window.location.pathname+window.location.search);return true;}return false;}
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

/* ── Image lightbox — click any .msg-media-img to enlarge ─────────────────── */
function _openImgLightbox(src, alt) {
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
  lb.onclick = () => _closeImgLightbox(lb);
  document.body.appendChild(lb);
  // Close on Escape
  lb._escHandler = e => { if(e.key==='Escape') _closeImgLightbox(lb); };
  document.addEventListener('keydown', lb._escHandler);
}
function _closeImgLightbox(lb) {
  if(!lb || !lb.parentNode) return;
  document.removeEventListener('keydown', lb._escHandler);
  lb.style.animation = 'lb-in .12s ease reverse';
  setTimeout(() => lb.parentNode && lb.parentNode.removeChild(lb), 120);
}

document.addEventListener('click', e => {
  const img = e.target && e.target.closest ? e.target.closest('.msg-media-img') : null;
  if(!img) return;
  _openImgLightbox(img.src, img.alt);
});

const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _PDF_EXTS=/\.pdf$/i;
const _HTML_EXTS=/\.(html?|htm)$/i;
const _ARCHIVE_EXTS=/\.(zip|tar|tar\.gz|tgz|tar\.bz2|tbz2|tar\.xz|txz)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
const _CSV_EXTS=/\.csv$/i;
const _EXCALIDRAW_EXTS=/\.excalidraw$/i;

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
  // Also strip @provider: prefix from deduplicated model IDs (#1228).
  const norm=s=>s.toLowerCase().replace(/^[^/]+\//,'').replace(/^@([^:]+:)+/,'').replace(/-/g,'.');
  const target=norm(modelId);
  const exact=opts.find(o=>norm(o)===target);
  if(exact) return exact;
  // 3. Prefix/substring: require the candidate to start with the FULL normalized target
  // (not a truncated base). This avoids false matches like gpt.5.5 → gpt.5.4.mini (#1188).
  // Only fall back to the shorter base form if target itself is very short (a bare root
  // like "gpt" or "claude") where stripping would be a no-op anyway.
  const base=target.replace(/\.\d+$/,'');  // strip trailing version number
  const useBase=base.length<=4||base===target; // bare root — stripping changed nothing meaningful
  const prefixTarget=useBase?base:target;
  const partial=opts.find(o=>norm(o).startsWith(prefixTarget));
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
    const _modelsRes=await fetch(new URL('api/models',location.href).href,{credentials:'include'});
    if(_redirectIfUnauth(_modelsRes)) return;
    const data=await _modelsRes.json();
    if(!data.groups||!data.groups.length) return; // keep HTML defaults
    // Store active provider globally so the send path can warn on mismatch
    window._activeProvider=data.active_provider||null;
    // Store default model so newSession() can apply it (#872).
    // Per-page-load — not synced across browser tabs.
    window._defaultModel=data.default_model||null;
    // Clear existing options
    sel.innerHTML='';
    _dynamicModelLabels={};
    for(const g of data.groups){
      const og=document.createElement('optgroup');
      og.label=g.provider;
      if(g.provider_id) og.dataset.provider=g.provider_id;
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
    sel.appendChild(providerGroup);
  }
  const existingIds=new Set([...sel.options].map(o=>o.value));
  // Normalized dedup: strip @provider: prefix and unify separators so
  // 'minimax/minimax-m2.7' matches '@nous:minimax/minimax-m2.7' (#907).
  // Strip ONLY the first colon — Ollama tag IDs are multi-colon
  // (e.g. '@ollama-cloud:qwen3-vl:235b-instruct') and split(':',2) would
  // truncate the tag suffix in JS (the limit arg discards extras, unlike Python).
  const _normId=id=>{
    let s=String(id||'');
    if(s.startsWith('@')&&s.includes(':')) s=s.substring(s.indexOf(':')+1); // strip only @provider:
    s=s.split('/').pop();                                                    // strip namespace prefix
    return s.replace(/-/g,'.').toLowerCase();
  };
  const existingNorm=new Set([...sel.options].map(o=>_normId(o.value)));
  let added=0;
  const _ap=(window._activeProvider||'').toLowerCase();
  const _isPortalFetch=_ap && _ap!=='openrouter' && _ap!=='custom' && provider===_ap;
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
    providerGroup.appendChild(opt);
    _dynamicModelLabels[mid]=m.label||m.id;
    added++;
  }
  if(added>0 && currentVal) _applyModelToDropdown(currentVal, sel);
  // After live models are added, re-apply the session's model in case it was
  // absent from the static list and syncTopbar() fired before the live fetch
  // completed (#1169). This ensures the session model wins over any premature
  // fallback that may have set sel.value to the first available option.
  if(S.session && S.session.model && sel.id==='modelSelect'){
    const reapplied=_applyModelToDropdown(S.session.model, sel);
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
    const url=new URL('api/models/live',location.href);
    url.searchParams.set('provider',provider);
    const _liveRes=await fetch(url.href,{credentials:'include'});
    if(_redirectIfUnauth(_liveRes)) return;
    const data=await _liveRes.json();
    if(!data.models||!data.models.length) return;
    _liveModelCache[provider]=data.models;
    const added=_addLiveModelsToSelect(provider,data.models,sel);
    if(added>0){
      if(typeof syncModelChip==='function') syncModelChip();
      console.log('[hermes] Live models loaded for',provider+':',added,'new models added');
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
 * name. Custom/local endpoints report active_provider='custom' or the
 * base_url hostname and we skip the check to avoid false positives.
 */
function _checkProviderMismatch(modelId){
  const ap=(window._activeProvider||'').toLowerCase();
  if(!ap||ap==='custom'||ap==='openrouter') return null; // can't reliably check
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
    dd.appendChild(_scopeNote);
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
  dd.appendChild(_scopeNote);
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
    opt.textContent=getModelLabel(value);
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
  if(typeof closeReasoningDropdown==='function') closeReasoningDropdown();
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
  // Keep the reasoning dropdown aligned under its chip when the window
  // resizes while open — same pattern as the model dropdown above.
  const rdd=$('composerReasoningDropdown');
  if(rdd&&rdd.classList.contains('open')&&typeof _positionReasoningDropdown==='function'){
    _positionReasoningDropdown();
  }
});

// ── Reasoning effort chip ────────────────────────────────────────────────────
let _currentReasoningEffort=null;

function _normalizeReasoningEffort(eff){
  return String(eff||'').trim().toLowerCase();
}

function _formatReasoningEffortLabel(effort){
  if(effort==='none') return 'None';
  if(!effort) return 'Default';
  return effort;
}

function _applyReasoningChip(eff){
  const effort=_normalizeReasoningEffort(eff);
  _currentReasoningEffort=effort;
  const wrap=$('composerReasoningWrap');
  const label=$('composerReasoningLabel');
  const chip=$('composerReasoningChip');
  if(!wrap||!label) return;
  wrap.style.display='';
  label.textContent=_formatReasoningEffortLabel(effort);
  if(chip){
    const inactive=!effort||effort==='none';
    chip.classList.toggle('inactive',inactive);
    chip.title='Reasoning effort: '+_formatReasoningEffortLabel(effort);
  }
  _highlightReasoningOption(effort);
}

function fetchReasoningChip(){
  api('/api/reasoning').then(function(st){
    _applyReasoningChip((st&&st.reasoning_effort)||'');
  }).catch(function(){_applyReasoningChip('');});
}

function syncReasoningChip(){
  if(_currentReasoningEffort===null){fetchReasoningChip();return;}
  _applyReasoningChip(_currentReasoningEffort);
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
  _highlightReasoningOption(_currentReasoningEffort);
  dd.classList.add('open');
  _positionReasoningDropdown();
  chip.classList.add('active');
}

function _positionReasoningDropdown(){
  const dd=$('composerReasoningDropdown');
  const chip=$('composerReasoningChip');
  const footer=document.querySelector('.composer-footer');
  if(!dd||!chip||!footer) return;
  const chipRect=chip.getBoundingClientRect();
  const footerRect=footer.getBoundingClientRect();
  let left=chipRect.left-footerRect.left;
  const maxLeft=Math.max(0,footer.clientWidth-dd.offsetWidth);
  left=Math.max(0,Math.min(left,maxLeft));
  dd.style.left=`${left}px`;
}

function closeReasoningDropdown(){
  const dd=$('composerReasoningDropdown');
  const chip=$('composerReasoningChip');
  if(dd) dd.classList.remove('open');
  if(chip) chip.classList.remove('active');
}

document.addEventListener('click',function(e){
  if(!e.target.closest('#composerReasoningChip')&&!e.target.closest('#composerReasoningDropdown')) closeReasoningDropdown();
  if(e.target.closest('.reasoning-option')){
    const opt=e.target.closest('.reasoning-option');
    const effort=opt&&opt.dataset.effort;
    if(effort){
      api('/api/reasoning',{method:'POST',body:JSON.stringify({effort:effort})})
        .then(function(st){
          _applyReasoningChip((st&&st.reasoning_effort)||effort);
          showToast('🧠 Reasoning effort set to '+((st&&st.reasoning_effort)||effort));
        })
        .catch(function(){showToast('🧠 Failed to set effort');});
      closeReasoningDropdown();
    }
  }
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
    // Load older messages when scrolled near the top
    if(el.scrollTop<80 && typeof _messagesTruncated!=='undefined' && _messagesTruncated && typeof _loadOlderMessages==='function'){
      _loadOlderMessages();
    }
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
  // ── Compress affordance (#524) ──
  // Show a hint in the tooltip when context usage is high so users
  // discover /compress without having to know the slash command.
  const compressWrap=$('ctxTooltipCompress');
  const compressBtn=$('ctxCompressBtn');
  if(compressWrap&&compressBtn){
    if(pct>=75){
      compressWrap.style.display='';
      compressBtn.textContent=t('ctx_compress_action');
      compressBtn.onclick=function(){
        const ta=$('msg');
        if(ta){ta.value='/compress ';ta.focus();autoResize();}
      };
    }else if(pct>=50){
      compressWrap.style.display='';
      compressBtn.textContent=t('ctx_compress_hint');
      compressBtn.onclick=function(){
        const ta=$('msg');
        if(ta){ta.value='/compress ';ta.focus();autoResize();}
      };
    }else{
      compressWrap.style.display='none';
    }
  }
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
  // Check dynamic labels first, then fall back to splitting the ID
  if(_dynamicModelLabels[modelId]) return _dynamicModelLabels[modelId];
  // Static fallback for common models
  const STATIC_LABELS={'openai/gpt-5.4-mini':'GPT-5.4 Mini','openai/gpt-4o':'GPT-4o','openai/o3':'o3','openai/o4-mini':'o4-mini','anthropic/claude-sonnet-4.6':'Sonnet 4.6','anthropic/claude-sonnet-4-5':'Sonnet 4.5','anthropic/claude-haiku-3-5':'Haiku 3.5','google/gemini-3.1-pro-preview':'Gemini 3.1 Pro','google/gemini-3-flash-preview':'Gemini 3 Flash','google/gemini-3.1-flash-lite-preview':'Gemini 3.1 Flash Lite','google/gemini-2.5-pro':'Gemini 2.5 Pro','google/gemini-2.5-flash':'Gemini 2.5 Flash','deepseek/deepseek-v4-flash':'DeepSeek V4 Flash','deepseek/deepseek-v4-pro':'DeepSeek V4 Pro','deepseek/deepseek-chat-v3-0324':'DeepSeek V3 (legacy)','meta-llama/llama-4-scout':'Llama 4 Scout'};
  if(STATIC_LABELS[modelId]) return STATIC_LABELS[modelId];
  // Safe Ollama-tag fallback formatter before generic split('/').pop()
  let _last = modelId.split('/').pop() || modelId;
  // Strip @provider: prefix if present (e.g. @ollama-cloud:kimi-k2.6)
  if (_last.startsWith('@') && _last.includes(':')) _last = _last.split(':').slice(1).join(':');
  const looksLikeOllamaTag = /^[a-z0-9][\w.-]*:[\w.-]+$/i.test(_last);
  // Narrow: only apply Ollama formatter to IDs with explicit @ollama prefix or colon-tag format.
  // Avoids reformatting bare provider model IDs like claude-sonnet-4-6 or gpt-4o.
  const looksLikeBareOllamaId = modelId.startsWith('@ollama') || looksLikeOllamaTag;
  const ollamaLabel = _fmtOllamaLabel(_last);
  if ((modelId.startsWith('ollama/') || modelId.startsWith('@ollama') || looksLikeOllamaTag || looksLikeBareOllamaId) && ollamaLabel !== _last) {
    return ollamaLabel;
  }
  return _last || 'Unknown';
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
    let inFence=false;     // inside a non-blockquote ```...``` fence
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
        if(/^```/.test(line)) inFence=false;
        continue;
      }
      if(/^```/.test(line)){
        flush(i);
        out.push(line);
        inFence=true;
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
  s=s.replace(/```([\s\S]*?)```/g,(_,raw)=>{
    const m=raw.match(/^(\w[\w+-]*)\n?([\s\S]*)$/);
    if(m&&m[1].trim().toLowerCase()==='mermaid'){
      const id='mermaid-'+Math.random().toString(36).slice(2,10);
      _preBlock_stash.push(`<div class="mermaid-block" data-mermaid-id="${id}">${esc(m[2].trim())}</div>`);
    } else {
      const lang=m?(m[1]||'').trim().toLowerCase():'';
      const code=m?m[2]:raw.replace(/^\n?/,'');
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
        const blockId='tree-'+Math.random().toString(36).slice(2,10);
        _preBlock_stash.push(`<div class="code-tree-wrap" data-raw="${rawCode.replace(/"/g,'&quot;')}" data-lang="${lang}" id="${blockId}">${h}<pre class="tree-raw-view"><code${langAttr}>${rawCode}</code></pre></div>`);
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
    return '\x00P'+(_preBlock_stash.length-1)+'\x00';
  });
  s=s.replace(/`([^`\n]+)`/g,(_,c)=>{fence_stash.push('<code>'+esc(c)+'</code>');return '\x00F'+(fence_stash.length-1)+'\x00';});
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
  // Stash raw <pre> blocks so the inline <code> rewrite below does not run
  // inside them. Running that rewrite in <pre> content can introduce stray
  // backticks for multiline code and break subsequent code-box rendering.
  const rawPreStash=[];
  s=s.replace(/(<pre\b[^>]*>[\s\S]*?<\/pre>)/gi,m=>{rawPreStash.push(m);return `\x00R${rawPreStash.length-1}\x00`;});
  s=s.replace(/<strong>([\s\S]*?)<\/strong>/gi,(_,t)=>'**'+t+'**');
  s=s.replace(/<b>([\s\S]*?)<\/b>/gi,(_,t)=>'**'+t+'**');
  s=s.replace(/<em>([\s\S]*?)<\/em>/gi,(_,t)=>'*'+t+'*');
  s=s.replace(/<i>([\s\S]*?)<\/i>/gi,(_,t)=>'*'+t+'*');
  s=s.replace(/<code>([^<]*?)<\/code>/gi,(_,t)=>'`'+t+'`');
  s=s.replace(/<br\s*\/?>/gi,'\n');
  s=s.replace(/\x00R(\d+)\x00/g,(_,i)=>rawPreStash[+i]);
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
    t=t.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g,(_,lb,u)=>{_link_stash.push(`<a href="${u.replace(/"/g,'%22')}" target="_blank" rel="noopener">${esc(lb)}</a>`);return `\x00L${_link_stash.length-1}\x00`;});
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
    const parseRow=r=>r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<td>${inlineMd(c.trim())}</td>`).join('');
    const parseHeader=r=>r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<th>${inlineMd(c.trim())}</th>`).join('');
    const header=`<tr>${parseHeader(rows[0])}</tr>`;
    const body=rows.slice(2).map(r=>`<tr>${parseRow(r)}</tr>`).join('');
    return `<table><thead>${header}</thead><tbody>${body}</tbody></table>`;
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
  s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g,(_,label,url)=>`<a href="${url.replace(/"/g,'%22')}" target="_blank" rel="noopener">${esc(label)}</a>`);
  s=s.replace(/\x00A(\d+)\x00/g,(_,i)=>_a_stash[+i]);
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
  function _isSafeUrl(v, img){
    const raw=_safeAttrValue(v);
    const compact=raw.replace(/[\u0000-\u001f\u007f\s]+/g,'').toLowerCase();
    if(!compact) return false;
    if(/^(javascript|data|vbscript):/i.test(compact)) return false;
    if(/^https?:\/\//i.test(raw)) return true;
    if(img && /^api\//i.test(raw)) return true;
    if(!img && (/^api\//i.test(raw) || /^#/.test(raw))) return true;
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
      const cls=_cls(a.class,['msg-media-link','skill-linked-file','skill-file-back']);
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
  s=s.replace(/(<div class="pre-header">[\s\S]*?<\/div>)?<pre>[\s\S]*?<\/pre>|<div class="(mermaid-block|katex-block)"[\s\S]*?<\/div>/g,m=>{
    _pre_stash.push(m);
    return '\x00E'+(_pre_stash.length-1)+'\x00';
  });
  const parts=s.split(/\n{2,}/);
  s=parts.map(p=>{p=p.trim();if(!p)return '';if(/^<(h[1-6]|ul|ol|pre|hr|blockquote)|^\x00[EQ]/.test(p))return p;return `<p>${p.replace(/\n/g,'<br>')}</p>`;}).join('\n');
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
      // SVG URLs → render inline as image (before image catch-all)
      if(_SVG_EXTS.test(src.split('?')[0])){
        return `<img class="msg-media-svg" src="${esc(src)}" alt="${t('media_svg_label')}" loading="lazy">`;
      }
      // Audio URLs → inline player
      if(_AUDIO_EXTS.test(src.split('?')[0])){
        return `<div class="msg-media-audio"><span class="msg-media-label">🎵 ${t('media_audio_label')}</span><audio controls preload="metadata" src="${esc(src)}"></audio></div>`;
      }
      // Video URLs → inline player
      if(_VIDEO_EXTS.test(src.split('?')[0])){
        return `<div class="msg-media-video"><span class="msg-media-label">🎬 ${t('media_video_label')}</span><video controls preload="metadata" src="${esc(src)}"></video></div>`;
      }
      // MEDIA: tokens are only emitted for tool-generated images (image_generate etc.).
      // Render all https:// URLs as <img> — extension check would miss extensionless
      // CDN paths like fal.media content-addressed URLs (closes #853).
      if(_IMAGE_EXTS.test(src.split('?')[0]) || /^https?:\/\//i.test(src)){
        return `<img class="msg-media-img" src="${esc(src)}" alt="image" loading="lazy">`;
      }
      return `<a href="${esc(src)}" target="_blank" rel="noopener">${esc(src)}</a>`;
    }
    // Local file path
    const apiUrl='api/media?path='+encodeURIComponent(ref);
    if(_IMAGE_EXTS.test(ref)){
      return `<img class="msg-media-img" src="${esc(apiUrl)}" alt="${esc(ref.split('/').pop())}" loading="lazy">`;
    }
    // SVG → inline image (no download, render directly)
    if(_SVG_EXTS.test(ref)){
      return `<img class="msg-media-svg" src="${esc(apiUrl)}" alt="${t('media_svg_label')}" loading="lazy">`;
    }
    // Audio → inline player
    if(_AUDIO_EXTS.test(ref)){
      return `<div class="msg-media-audio"><span class="msg-media-label">🎵 ${t('media_audio_label')}</span><audio controls preload="metadata" src="${esc(apiUrl)}"></audio></div>`;
    }
    // Video → inline player
    if(_VIDEO_EXTS.test(ref)){
      return `<div class="msg-media-video"><span class="msg-media-label">🎬 ${t('media_video_label')}</span><video controls preload="metadata" src="${esc(apiUrl)}"></video></div>`;
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
    const _dcompr=typeof isCompressionUiRunning==='function'&&isCompressionUiRunning();
    if(_dmsg&&_dmsg.disabled) _btnTitle=_tt('composer_disabled_clarify','Respond to the clarification request');
    else if(_dcompr) _btnTitle=_tt('composer_disabled_compression','Waiting for compression to finish');
    else _btnTitle=_tt('composer_disabled_empty','Type a message to send');
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
        $('msg').value=next.text||'';
        S.pendingFiles=Array.isArray(next.files)?[...next.files]:[];
        // Restore model from queued item (sent in /api/chat/start payload)
        // Note: profile is NOT restored — full profile switch requires server interaction
        if(next.model&&S.session&&next.model!==S.session.model){
          S.session.model=next.model;
          if(typeof _applyModelToDropdown==='function'&&$('modelSelect')) _applyModelToDropdown(next.model,$('modelSelect'));
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
      if(typeof scrollToBottom==='function') scrollToBottom();
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
        const firstFiles=(snapshot.find(e=>e&&Array.isArray(e.files)&&e.files.length)||{files:[]}).files;
        liveQ.length=0;liveQ.push({text:combined,files:firstFiles,_queued_at:Date.now()});
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
      if(typeof scrollToBottom==='function') scrollToBottom();
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
function showToast(msg,ms,type){const el=$('toast');if(!el)return;const s=String(msg==null?'':msg);let t=type;if(!t){const low=s.toLowerCase();if(/fail|error|denied|invalid|unavailable|no active|no workspace match|no model match|no personalities/.test(low))t='error';else if(/warn|queued|takes effect|skipped|fallback/.test(low))t='warning';else if(/saved|created|imported|restored|switched|set to|updated|duplicated|moved to|renamed|deleted|complete|pinned|archived|cleared|stopped/.test(low))t='success';else t='info';}el.textContent=s;el.className='toast show '+t;clearTimeout(el._t);el._t=setTimeout(()=>{el.classList.remove('show');},ms||2800);}

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
function copyMsg(btn){
  const row=btn.closest('[data-raw-text]');
  const text=row?row.dataset.rawText:'';
  if(!text)return;
  _copyText(text).then(()=>{
    const orig=btn.innerHTML;btn.innerHTML=li('check',13);btn.style.color='var(--blue)';
    setTimeout(()=>{btn.innerHTML=orig;btn.style.color='';},1500);
  }).catch(()=>showToast(t('copy_failed')));
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
  const errEl=$('updateError');
  if(errEl){errEl.style.display='none';errEl.textContent='';}
  // Hide any leftover force-update button from a prior conflict so a fresh
  // retry starts clean (otherwise stale state points at the wrong target).
  const forceBtnReset=$('btnForceUpdate');
  if(forceBtnReset){forceBtnReset.style.display='none';forceBtnReset.dataset.target='';}
  const targets=[];
  if(window._updateData?.webui?.behind>0) targets.push('webui');
  if(window._updateData?.agent?.behind>0) targets.push('agent');
  try{
    for(const target of targets){
      const res=await api('/api/updates/apply',{method:'POST',body:JSON.stringify({target})});
      if(!res.ok){
        _showUpdateError(target,res);
        if(btn){btn.disabled=false;btn.textContent='Update Now';}
        return;
      }
    }
    showToast('Update applied — restarting…');
    sessionStorage.removeItem('hermes-update-checked');
    sessionStorage.removeItem('hermes-update-dismissed');
    _waitForServerThenReload();
  }catch(e){
    if(errEl){errEl.textContent='Update failed: '+e.message;errEl.style.display='block';}
    else showToast('Update failed: '+e.message);
    if(btn){btn.disabled=false;btn.textContent='Update Now';}
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
    const res=await api('/api/updates/force',{method:'POST',body:JSON.stringify({target})});
    if(!res.ok){
      if(errEl){errEl.textContent='Force update failed: '+(res.message||'unknown error');errEl.style.display='block';}
      btn.disabled=false;btn.textContent='Force update';
      return;
    }
    showToast('Force update applied — restarting…');
    sessionStorage.removeItem('hermes-update-checked');
    sessionStorage.removeItem('hermes-update-dismissed');
    _waitForServerThenReload();
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
      const r=await fetch('/health',{cache:'no-store'});
      if(r.ok){
        let data={};
        try{ data=await r.json(); }catch(_){}
        if(data && data.status==='ok'){
          location.reload();
          return;
        }
      }
    }catch(_){ /* socket closed during restart — retry */ }
    await new Promise(r=>setTimeout(r, interval));
  }
  if(msgEl) msgEl.textContent='⚠️ Server is taking longer than expected — click Reload when ready';
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
  document.title=sessionTitle+' \u2014 '+(window._botName||'Hermes');
  const vis=S.messages.filter(m=>m&&m.role&&m.role!=='tool');
  const _topbarMeta=$('topbarMeta');if(_topbarMeta)_topbarMeta.textContent=t('n_messages',vis.length);
  if(typeof syncAppTitlebar==='function') syncAppTitlebar();
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
    // If the model isn't in the current provider list, silently reset to the
    // first available model so stale values don't pollute the picker (#829).
    if(!applied && currentModel){
      const deferModelCorrection=Boolean(S.session._modelResolutionDeferred);
      // Also defer if a live model fetch is still in flight — the model may be
      // in the list once the fetch completes. Persisting now would corrupt the
      // session with the wrong model before live models arrive (#1169).
      const liveStillPending=window._activeProvider&&_liveModelFetchPending.has(window._activeProvider);
      if(liveStillPending){
        // Live fetch in flight — don't touch sel.value or S.session.model yet.
        // _addLiveModelsToSelect() will re-apply S.session.model once done (#1169).
      } else {
        // Stale session model not in the current provider catalog — reset to the
        // first available model rather than injecting an "(unavailable)" option
        // that visually appears under the wrong provider group (#829).
        const modelSel=$('modelSelect');
        const first=modelSel&&modelSel.querySelector('optgroup > option, option');
        if(first){
          modelSel.value=first.value;
          if(!deferModelCorrection){
            S.session.model=first.value;
            // Persist the correction so the session doesn't re-inject on next load.
            fetch(new URL('api/session/update',location.href).href,{
              method:'POST',credentials:'include',
              headers:{'Content-Type':'application/json'},
              body:JSON.stringify({session_id:S.session.id||S.session.session_id,model:first.value})
            }).catch(()=>{});
          }
        }
      }
    }
  }
  if(typeof syncModelChip==='function') syncModelChip();
  if(typeof syncReasoningChip==='function') syncReasoningChip();
  // Show Clear button only when session has messages
  const clearBtn=$('btnClearConv');
  if(clearBtn) clearBtn.style.display=(S.messages&&S.messages.filter(msg=>msg.role!=='tool').length>0)?'':'none';
  if(typeof _syncHermesPanelSessionActions==='function') _syncHermesPanelSessionActions();
  if(typeof syncWorkspaceDisplays==='function') syncWorkspaceDisplays();
  if(typeof syncTerminalButton==='function') syncTerminalButton();
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
  const clean=_sanitizeThinkingDisplayText(text);
  return `<div class="thinking-card"><div class="thinking-card-header" onclick="this.parentElement.classList.toggle('open')"><span class="thinking-card-icon">${li('lightbulb',14)}</span><span class="thinking-card-label">${t('thinking')}</span><span class="thinking-card-toggle">${li('chevron-right',12)}</span></div><div class="thinking-card-body"><pre>${esc(clean)}</pre></div></div>`;
}
function isSimplifiedToolCalling(){
  return window._simplifiedToolCalling!==false;
}
function _thinkingActivityNode(text){
  const row=document.createElement('div');
  row.className='agent-activity-thinking';
  row.innerHTML=_thinkingCardHtml(text);
  return row;
}
function ensureActivityGroup(inner, opts){
  opts=opts||{};
  if(!inner) return null;
  const live=!!opts.live;
  const selector=live?'.tool-call-group[data-live-tool-call-group="1"]':'.tool-call-group[data-agent-activity-group="1"]';
  let group=inner.querySelector(selector);
  if(!group){
    group=document.createElement('div');
    const collapsed=opts.collapsed!==false;
    group.className='tool-call-group agent-activity-group'+(collapsed?' tool-call-group-collapsed':'');
    group.setAttribute('data-tool-call-group','1');
    group.setAttribute('data-agent-activity-group','1');
    if(live) group.setAttribute('data-live-tool-call-group','1');
    group.innerHTML=`<button type="button" class="tool-call-group-summary" aria-expanded="${collapsed?'false':'true'}" onclick="const g=this.closest('.tool-call-group');const c=g.classList.toggle('tool-call-group-collapsed');this.setAttribute('aria-expanded',String(!c));"><span class="tool-call-group-chevron">${li('chevron-right',12)}</span><span class="tool-call-group-label">Activity</span><span class="tool-call-group-list">tools / thinking</span><span class="tool-call-group-count">0</span></button><div class="tool-call-group-body"></div>`;
    const anchor=opts.anchor||null;
    if(anchor&&anchor.parentElement===inner) anchor.insertAdjacentElement('afterend', group);
    else inner.appendChild(group);
  }
  _syncToolCallGroupSummary(group);
  return group;
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
function _autoCompressionCardsHtml(state){
  const fallback='Context auto-compressed to continue the conversation';
  const detail=String(state.message||fallback).trim()||fallback;
  const preview=String(state.summary?.headline||detail).trim()||detail;
  return `
    <div class="tool-card-row compression-card-row" data-compression-card="1">
      ${_compressionStatusCardHtml({
        statusLabel: t('auto_compress_label'),
        previewText: preview,
        detail,
        icon: li('check',13),
        open: false,
        variantClass: 'tool-card-compress-complete tool-card-compress-auto',
      })}
    </div>`;
}
function _compressionCardsNode(state){
  const wrap=document.createElement('div');
  wrap.className='compression-turn';
  wrap.innerHTML=`<div class="compression-turn-blocks">${_compressionCardsHtml(state)}</div>`;
  return wrap;
}
function _isContextCompactionMessage(m){
  if(!m||!m.role||m.role==='tool') return false;
  const text=msgContent(m)||String(m.content||'');
  return /^\s*\[context compaction/i.test(text) || /^\s*context compaction/i.test(text);
}
function _isPreservedCompressionTaskListMessage(m){
  if(!m||m.role!=='user') return false;
  const text=msgContent(m)||String(m.content||'');
  return /^\s*\[your active task list was preserved across context compression\]/i.test(text);
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
function _latestPreservedCompressionTaskListMessages(messages){
  const latest=[...(messages||[])].reverse().find(m=>_isPreservedCompressionTaskListMessage(m));
  return latest?[latest]:[];
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
// to a session that was already rendered with the same message count.
// Keyed by session_id. Only used on cross-session navigation, never for
// in-session updates (new messages, edits, stream events).
//
// Known limitation: cache key is session_id + message count. Edits and retries
// that mutate message content without changing the count will serve stale HTML
// on back-navigation until the user triggers an in-session update. Acceptable
// for the common read-only back-navigation case; not suitable as a general cache.
const _sessionHtmlCache=new Map();
let _sessionHtmlCacheSid=null; // session_id currently rendered in the DOM
function clearMessageRenderCache(){
  _sessionHtmlCache.clear();
  _sessionHtmlCacheSid=null;
}

function renderMessages(){
  const inner=$('msgInner');
  const sid=S.session?S.session.session_id:null;
  const msgCount=S.messages.length;

  // Fast path: switching back to a previously rendered session with same count.
  // Guard: sid !== _sessionHtmlCacheSid ensures in-session updates (edits,
  // new messages, tool_complete) always get a fresh rebuild.
  // Skip cache if this session is still streaming — the live smd parser writes
  // into a DOM node inside the cached subtree; serving cached HTML detaches it.
  if(sid&&sid!==_sessionHtmlCacheSid&&!INFLIGHT[sid]){
    const cached=_sessionHtmlCache.get(sid);
    if(cached&&cached.msgCount===msgCount){
      inner.innerHTML=cached.html;
      _sessionHtmlCacheSid=sid;
      if(S.activeStreamId){scrollIfPinned();}else{scrollToBottom();}
      requestAnimationFrame(()=>{highlightCode();addCopyButtons();loadDiffInline();loadCsvInline();loadExcalidrawInline();loadPdfInline();loadHtmlInline();renderMermaidBlocks();renderKatexBlocks();});
      requestAnimationFrame(()=>{highlightCode();addCopyButtons();initTreeViews();loadPdfInline();loadHtmlInline();renderMermaidBlocks();renderKatexBlocks();});
      if(typeof loadTodos==='function'&&document.getElementById('panelTodos')&&document.getElementById('panelTodos').classList.contains('active')){loadTodos();}
      return;
    }
  }

  const compressionState=_compressionStateForCurrentSession();
  if(window._compressionUi && !compressionState) clearCompressionUi();
  const sessionCompressionAnchor=(
    S.session && typeof S.session.compression_anchor_visible_idx==='number'
  ) ? S.session.compression_anchor_visible_idx : null;
  const sessionCompressionAnchorKey=(
    S.session && S.session.compression_anchor_message_key && typeof S.session.compression_anchor_message_key==='object'
  ) ? S.session.compression_anchor_message_key : null;
  const preservedCompressionTaskMessages=_latestPreservedCompressionTaskListMessages(S.messages);
  const vis=S.messages.filter(m=>{
    if(!m||!m.role||m.role==='tool')return false;
    if(_isContextCompactionMessage(m)) return false;
    if(_isPreservedCompressionTaskListMessage(m)) return false;
    if(m.role==='assistant'){
      const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
      const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
      if(hasTc||hasTu||_messageHasReasoningPayload(m)) return true;
    }
    return msgContent(m)||m.attachments?.length;
  });
  $('emptyState').style.display=(vis.length||preservedCompressionTaskMessages.length)?'none':'';
  inner.innerHTML='';
  // Show "load older" indicator when older messages are available
  if(typeof _messagesTruncated!=='undefined' && _messagesTruncated && S.messages.length>0){
    const indicator=document.createElement('div');
    indicator.id='loadOlderIndicator';
    indicator.className='load-older-indicator';
    indicator.textContent=typeof t==='function'?t('load_older_messages'):'↑ Scroll up or click to load older messages';
    indicator.onclick=()=>{if(typeof _loadOlderMessages==='function') _loadOlderMessages();};
    inner.appendChild(indicator);
  }
  const compressionNode=compressionState?_compressionCardsNode(compressionState):null;
  const referenceMessage=S.messages.find(m=>_isContextCompactionMessage(m));
  const referenceText=referenceMessage?msgContent(referenceMessage)||String(referenceMessage.content||''):'';
  const referenceNode=(!compressionState && referenceMessage && (sessionCompressionAnchor!==null || sessionCompressionAnchorKey))
    ? (()=>{const row=document.createElement('div');row.innerHTML=`<div class="compression-turn"><div class="compression-turn-blocks">${_compressionReferenceCardHtml(referenceText,false)}${_preservedCompressionTaskListCardsHtml(preservedCompressionTaskMessages)}</div></div>`;return row.firstElementChild;})()
    : null;
  let preservedCompressionTaskCardsAttached=!!referenceNode;
  const visWithIdx=[];
  const preservedCompressionRawIdxs=[];
  let rawIdx=0;
  for(const m of S.messages){
    if(!m||!m.role||m.role==='tool'){rawIdx++;continue;}
    if(_isPreservedCompressionTaskListMessage(m)){preservedCompressionRawIdxs.push(rawIdx);rawIdx++;continue;}
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
  const assistantThinking=new Map();
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
      const _attachSid=(S.session&&S.session.session_id)||'';
      filesHtml=`<div class="msg-files">${m.attachments.map(f=>{
        const fLabel=typeof f==='string'?f:(f&&(f.name||f.filename||f.path))||'';
        const fname=String(fLabel).split('/').pop()||String(fLabel);
        if(_IMAGE_EXTS.test(fname)){
          // Use api/file/raw which resolves filename relative to the session workspace.
          // api/media expects a full absolute path which we don't store on the client side.
          const imgUrl='api/file/raw?session_id='+encodeURIComponent(_attachSid)+'&path='+encodeURIComponent(fname);
          return `<img class="msg-media-img" src="${esc(imgUrl)}" alt="${esc(fname)}" loading="lazy">`;
        }
        if(_SVG_EXTS.test(fname)){
          const svgUrl='api/file/raw?session_id='+encodeURIComponent(_attachSid)+'&path='+encodeURIComponent(fname);
          return `<img class="msg-media-svg" src="${esc(svgUrl)}" alt="${esc(fname)}" loading="lazy">`;
        }
        if(_AUDIO_EXTS.test(fname)){
          const audioUrl='api/file/raw?session_id='+encodeURIComponent(_attachSid)+'&path='+encodeURIComponent(fname);
          return `<div class="msg-media-audio"><span class="msg-media-label">🎵 ${esc(fname)}</span><audio controls preload="metadata" src="${esc(audioUrl)}"></audio></div>`;
        }
        if(_VIDEO_EXTS.test(fname)){
          const videoUrl='api/file/raw?session_id='+encodeURIComponent(_attachSid)+'&path='+encodeURIComponent(fname);
          return `<div class="msg-media-video"><span class="msg-media-label">🎬 ${esc(fname)}</span><video controls preload="metadata" src="${esc(videoUrl)}"></video></div>`;
        }
        return `<div class="msg-file-badge">${li('paperclip',12)} ${esc(fname)}</div>`;
      }).join('')}</div>`;
    }
    const bodyHtml = isUser ? esc(String(content)).replace(/\n/g,'<br>') : renderMd(_stripXmlToolCallsDisplay(String(content)));
    const isEditableUser=isUser&&rawIdx===lastUserRawIdx;
    const editBtn  = isEditableUser ? `<button class="msg-action-btn" title="${t('edit_message')}" onclick="editMessage(this)">${li('pencil',13)}</button>` : '';
    const undoBtn  = isLastAssistant ? `<button class="msg-action-btn" title="${t('undo_exchange')}" onclick="undoLastExchange()">${li('undo',13)}</button>` : '';
    const retryBtn = isLastAssistant ? `<button class="msg-action-btn" title="${t('regenerate')}" onclick="regenerateResponse(this)">${li('rotate-ccw',13)}</button>` : '';
    const copyBtn  = `<button class="msg-copy-btn msg-action-btn" title="${t('copy')}" onclick="copyMsg(this)">${li('copy',13)}</button>`;
    const tsVal=m._ts||m.timestamp;
    // _formatInServerTz handles fractional-hour offsets (India +0530 etc.)
    // correctly via offset arithmetic; bare toLocaleString is the browser-tz fallback.
    const _fmtSv=(typeof _formatInServerTz==='function')?_formatInServerTz:null;
    const tsTitle=tsVal?(_fmtSv?_fmtSv(new Date(tsVal*1000),{}):new Date(tsVal*1000).toLocaleString()):'';
    const tsTime=_formatMessageFooterTimestamp(tsVal);
    const timeHtml = tsTime ? `<span class="msg-time" title="${esc(tsTitle)}">${tsTime}</span>` : '';
    const footHtml = `<div class="msg-foot">${timeHtml}<span class="msg-actions">${editBtn}${copyBtn}${retryBtn}</span></div>`;

    if(_isContextCompactionMessage(m)){
      if(compressionState || referenceNode){
        continue;
      }else{
        currentAssistantTurn=null;
        const row=document.createElement('div');
        const preservedForThisCard=preservedCompressionTaskCardsAttached?[]:preservedCompressionTaskMessages;
        row.innerHTML=_contextCompactionMessageHtml(m, tsTitle, preservedForThisCard);
        if(preservedForThisCard.length) preservedCompressionTaskCardsAttached=true;
        inner.appendChild(row.firstElementChild);
        continue;
      }
    }

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
    if(thinkingText&&window._showThinking!==false){
      if(isSimplifiedToolCalling()) assistantThinking.set(rawIdx, thinkingText);
      else if(window._showThinking!==false) seg.insertAdjacentHTML('beforeend', _thinkingCardHtml(thinkingText));
    }
    const hasVisibleBody=!!(String(content||'').trim()||filesHtml);
    if(hasVisibleBody){
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
    if(anchorIdx!==null && visWithIdx[anchorIdx]){
      const anchorRawIdx=visWithIdx[anchorIdx].rawIdx;
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
  const preservedOnlyNode=(!preservedCompressionTaskCardsAttached&&(!referenceMessage||compressionState)&&preservedCompressionTaskMessages.length)
    ? (()=>{const row=document.createElement('div');row.innerHTML=`<div class="compression-turn"><div class="compression-turn-blocks">${_preservedCompressionTaskListCardsHtml(preservedCompressionTaskMessages)}</div></div>`;return row.firstElementChild;})()
    : null;
  const preservedOnlyAnchor=preservedCompressionRawIdxs.length
    ? (()=>{let idx=null;for(let i=0;i<visWithIdx.length;i++){if(visWithIdx[i].rawIdx<preservedCompressionRawIdxs[0]) idx=i;}return idx;})()
    : null;

  _insertCompressionLikeNode(compressionNode);
  _insertCompressionLikeNode(referenceNode);
  _insertCompressionLikeNode(preservedOnlyNode, preservedOnlyAnchor);
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
  if(!S.busy){
    inner.querySelectorAll('.tool-call-group:not([data-compression-card]),.tool-card-row:not([data-compression-card])').forEach(el=>el.remove());
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
        let anchorRow=assistantSegments.get(aIdx)||null;
        if(!anchorRow&&assistantIdxs.length){
          const fallbackIdx=[...assistantIdxs].reverse().find(idx=>idx<=aIdx);
          anchorRow=fallbackIdx!==undefined?assistantSegments.get(fallbackIdx):assistantSegments.get(assistantIdxs[assistantIdxs.length-1]);
        }
        if(!anchorRow) continue;
        const anchorParent=anchorRow.parentElement;
        const insertAfterNode = anchorInsertAfter.get(anchorRow) || anchorRow;
        const group=ensureActivityGroup(anchorParent,{collapsed:true,anchor:insertAfterNode});
        const body=group&&group.querySelector('.tool-call-group-body');
        if(!body) continue;
        const thinkingText=assistantThinking.get(aIdx);
        if(thinkingText) body.appendChild(_thinkingActivityNode(thinkingText));
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
  // Render per-turn token usage on each assistant message that has it (#503).
  // Replaces the old cumulative-total-on-last-bubble approach.
  if(window._showTokenUsage){
    const asstRows=inner.querySelectorAll('.assistant-turn');
    let ai=0; // assistant-only index for DOM rows
    for(let mi=0;mi<S.messages.length;mi++){
      const msg=S.messages[mi];
      if(msg.role!=='assistant'){continue;}
      if(!msg._turnUsage){ai++;continue;}
      if(ai>=asstRows.length) continue;
      const row=asstRows[ai];
      const footerRows=row.querySelectorAll('.msg-foot');
      const targetFoot=footerRows.length?footerRows[footerRows.length-1]:null;
      if(!targetFoot||targetFoot.querySelector('.msg-usage-inline')){ai++;continue;}
      const usage=document.createElement('span');
      usage.className='msg-usage-inline';
      const inTok=msg._turnUsage.input_tokens||0;
      const outTok=msg._turnUsage.output_tokens||0;
      const cost=msg._turnUsage.estimated_cost;
      let text=`${_fmtTokens(inTok)} in · ${_fmtTokens(outTok)} out`;
      if(cost) text+=` · ~$${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
      usage.textContent=text;
      targetFoot.classList.add('msg-foot-with-usage');
      targetFoot.insertBefore(usage, targetFoot.firstChild);
      ai++;
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
  requestAnimationFrame(()=>{highlightCode();addCopyButtons();loadDiffInline();loadCsvInline();loadExcalidrawInline();loadPdfInline();loadHtmlInline();renderMermaidBlocks();renderKatexBlocks();});
  requestAnimationFrame(()=>{highlightCode();addCopyButtons();initTreeViews();loadPdfInline();loadHtmlInline();renderMermaidBlocks();renderKatexBlocks();}); 
  // Refresh todo panel if it's currently open
  if(typeof loadTodos==='function' && document.getElementById('panelTodos') && document.getElementById('panelTodos').classList.contains('active')){
    loadTodos();
  }
  // Populate session cache so switching back here skips a full rebuild.
  _sessionHtmlCacheSid=sid;
  if(sid){
    const _html=inner.innerHTML;
    // Only cache sessions with <300KB rendered HTML; evict oldest beyond 8 sessions.
    if(_html.length<300_000){
      _sessionHtmlCache.set(sid,{html:_html,msgCount});
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
    if(s.length<=800){displaySnippet=s;}
    else{
      const cutoff=s.slice(0,800);
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
  let displayName=_toolDisplayName(tc);
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

function _syncToolCallGroupSummary(group){
  if(!group) return;
  const cards=Array.from(group.querySelectorAll('.tool-card-row .tool-card'));
  const toolCount=cards.length;
  const thinkingCount=group.querySelectorAll('.agent-activity-thinking .thinking-card').length;
  const names=cards.map(card=>{
    const el=card.querySelector('.tool-card-name');
    return el?String(el.textContent||'').trim():'';
  }).filter(Boolean);
  const uniqueNames=[...new Set(names)];
  const label=group.querySelector('.tool-call-group-label');
  const list=group.querySelector('.tool-call-group-list');
  const badge=group.querySelector('.tool-call-group-count');
  const parts=[];
  if(thinkingCount) parts.push('thinking');
  if(uniqueNames.length) parts.push(uniqueNames.slice(0,5).join(', ')+(uniqueNames.length>5?'…':''));
  const total=toolCount+thinkingCount;
  if(label){
    if(thinkingCount&&toolCount) label.textContent=`Activity: thinking + ${toolCount} tool${toolCount===1?'':'s'}`;
    else if(thinkingCount) label.textContent='Activity: thinking';
    else if(toolCount) label.textContent=`Activity: ${toolCount} tool${toolCount===1?'':'s'}`;
    else label.textContent='Activity';
  }
  if(list) list.textContent=parts.join(' · ')||'tools / thinking';
  if(badge) badge.textContent=String(total);
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
  const group=ensureActivityGroup(inner,{live:true,collapsed:false,anchor});
  const body=group.querySelector('.tool-call-group-body');
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
  const inner=_assistantTurnBlocks($('liveAssistantTurn'));
  if(inner) inner.querySelectorAll('.tool-call-group[data-live-tool-call-group],.tool-card-row[data-live-tid]').forEach(el=>el.remove());
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

// Lazy load js-yaml for YAML tree view support
let _jsyamlLoading=false;
function _loadJsyamlThen(cb){
  if(typeof jsyaml!=='undefined'){ cb(); return; }
  if(_jsyamlLoading){ setTimeout(()=>_loadJsyamlThen(cb),100); return; }
  _jsyamlLoading=true;
  const s=document.createElement('script');
  s.src='https://cdnjs.cloudflare.com/ajax/libs/js-yaml/4.1.0/js-yaml.min.js';
  s.integrity='sha384-8pLvVQkv7pCQqFk7AChLpdEe7gXz9h8GAb7cS0zVeJuKhxR5PU5aEET5pRpHZvxUorzdM';
  s.crossOrigin='anonymous';
  s.onload=()=>{ _jsyamlLoading=false; cb(); };
  s.onerror=()=>{ _jsyamlLoading=false; }; // CDN blocked, fall back to raw
  document.head.appendChild(s);
}

function initTreeViews(){
  document.querySelectorAll('.code-tree-wrap:not([data-tree-init])').forEach(wrap=>{
    wrap.setAttribute('data-tree-init','1');
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
        // Trigger async load, leave as raw for now
        parseFailed=true;
      }
    }
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
    if(pre.querySelector('.code-copy-btn')) return;
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

function loadDiffInline(){
  const DIFF_MAX_SIZE=512*1024; // 512 KB cap for inline diff rendering
  document.querySelectorAll('.diff-inline-load:not([data-loaded])').forEach(el=>{
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

function loadCsvInline(){
  const CSV_MAX_SIZE=256*1024; // 256 KB cap for inline CSV rendering
  document.querySelectorAll('.csv-inline-load:not([data-loaded])').forEach(el=>{
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

function loadExcalidrawInline(){
  const EXCALIDRAW_MAX_SIZE=512*1024; // 512 KB cap
  document.querySelectorAll('.excalidraw-inline-load:not([data-loaded])').forEach(el=>{
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

// ── PDF inline preview (first page via PDF.js CDN) ──────────────────────────
let _pdfjsReady=false, _pdfjsLoading=false;
function loadPdfInline(){
  const PDF_MAX_SIZE=4*1024*1024; // 4 MB cap for inline PDF preview
  document.querySelectorAll('.pdf-preview-load:not([data-loaded])').forEach(el=>{
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
          return pdfjsLib.getDocument({data:buf}).promise;
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
      const s=document.createElement('script');
      s.src='https://cdn.jsdelivr.net/npm/pdfjs-dist@4.9.155/build/pdf.min.mjs';
      s.type='module';
      s.textContent=`
        import * as pdfjsLib from '${s.src}';
        pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdn.jsdelivr.net/npm/pdfjs-dist@4.9.155/build/pdf.worker.min.mjs';
        window._pdfjsLib=pdfjsLib;
        window._pdfjsReady=true;
        window.dispatchEvent(new Event('pdfjs-ready'));
      `;
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
function loadHtmlInline(){
  const HTML_MAX_SIZE=256*1024; // 256 KB cap for inline HTML preview
  document.querySelectorAll('.html-preview-load:not([data-loaded])').forEach(el=>{
    el.setAttribute('data-loaded','1');
    const path=el.dataset.path;
    const fname=path.split('/').pop()||path;
    fetch('api/media?path='+encodeURIComponent(path))
      .then(r=>{if(!r.ok) throw new Error(r.status); return r.text();})
      .then(html=>{
        if(html.length>HTML_MAX_SIZE){
          const dlUrl='api/media?path='+encodeURIComponent(path)+'&download=1';
          el.outerHTML=`<div class="html-preview-fallback"><a class="msg-media-link" href="${dlUrl}" download="${esc(fname)}">📎 ${esc(fname)}</a><br><span style="color:var(--muted);font-size:12px">${t('html_too_large')}</span></div>`;
          return;
        }
        const dlUrl='api/media?path='+encodeURIComponent(path)+'&download=1';
        const safeHtml=html.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        el.outerHTML=`<div class="html-preview-wrap"><div class="html-preview-header"><span>${t('html_sandbox_label')}</span><a href="${dlUrl}" download="${esc(fname)}" class="html-open-link">${t('html_open_full')} ↗</a></div><iframe srcdoc="${safeHtml}" sandbox="allow-scripts" class="html-preview-iframe" loading="lazy"></iframe></div>`;
      })
      .catch(()=>{
        const dlUrl='api/media?path='+encodeURIComponent(path)+'&download=1';
        el.outerHTML=`<div class="html-preview-fallback"><a class="msg-media-link" href="${dlUrl}" download="${esc(fname)}">📎 ${esc(fname)}</a><br><span style="color:var(--muted);font-size:12px">${t('html_error')}</span></div>`;
      });
  });
}

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
  const clean=_sanitizeThinkingDisplayText(text);
  const openClass=isSimplifiedToolCalling()?'':' open';
  return (clean&&String(clean).trim())
    ? `<div class="thinking-card${openClass}"><div class="thinking-card-header" onclick="this.parentElement.classList.toggle('open')"><span class="thinking-card-icon">${li('lightbulb',14)}</span><span class="thinking-card-label">${t('thinking')}</span><span class="thinking-card-toggle">${li('chevron-right',12)}</span></div><div class="thinking-card-body"><pre>${esc(String(clean).trim())}</pre></div></div>`
    : `<div class="thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>`;
}
function finalizeThinkingCard(){
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
    group.classList.add('tool-call-group-collapsed');
    const summary=group.querySelector('.tool-call-group-summary');
    if(summary) summary.setAttribute('aria-expanded','false');
    const active=group.querySelector('.agent-activity-thinking[data-thinking-active="1"]');
    if(active) active.removeAttribute('data-thinking-active');
    _syncToolCallGroupSummary(group);
  }
}
function appendThinking(text=''){
  // Guard: ignore if session was switched during an async SSE stream.
  // The old stream's reasoning events can still fire after switch;
  // without this check they would pollute the new session's DOM.
  if(!S.session||!S.activeStreamId) return;
  $('emptyState').style.display='none';
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
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
    row.className=(text&&String(text).trim())?'assistant-segment thinking-card-row':'assistant-segment';
    row.innerHTML=_thinkingMarkup(text);
    scrollIfPinned();
    // Auto-scroll the thinking card body to bottom if the user is watching
    // (scroll pinned). If the user scrolled up to read history, leave it alone.
    if(_scrollPinned){
      const body=row&&row.querySelector('.thinking-card-body');
      if(body) body.scrollTop=body.scrollHeight;
    }
    return;
  }
  if(!String(text||'').trim()){
    scrollIfPinned();
    return;
  }
  const allChildren=Array.from(blocks.children);
  const anchor=allChildren.filter(el=>
    el.id!=='toolRunningRow' &&
    el.matches('[data-live-assistant="1"],.tool-call-group,.tool-card-row,.agent-activity-thinking')
  ).pop();
  const group=ensureActivityGroup(blocks,{live:true,collapsed:true,anchor});
  const body=group&&group.querySelector('.tool-call-group-body');
  if(!body) return;
  let row=body.querySelector('.agent-activity-thinking[data-thinking-active="1"]');
  if(!row){
    row=document.createElement('div');
    row.className='agent-activity-thinking';
    row.setAttribute('data-thinking-active','1');
    body.insertBefore(row, body.firstChild);
  }
  row.innerHTML=_thinkingMarkup(text);
  _syncToolCallGroupSummary(group);
  scrollIfPinned();
  if(_scrollPinned){
    const thinkingBody=row&&row.querySelector('.thinking-card-body');
    if(thinkingBody) thinkingBody.scrollTop=thinkingBody.scrollHeight;
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
    if(!group.querySelector('.tool-card-row,.agent-activity-thinking')) group.remove();
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
    el.setAttribute('draggable','true');
    el.oncontextmenu=(e)=>{e.preventDefault();e.stopPropagation();_showFileContextMenu(e,item);};
    el.ondragstart=(e)=>{e.dataTransfer.setData('application/ws-path',item.path);e.dataTransfer.setData('application/ws-type',item.type);e.dataTransfer.effectAllowed='copy';};

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
  renameItem.onmouseenter=()=>renameItem.style.background='var(--hover)';
  renameItem.onmouseleave=()=>renameItem.style.background='';
  renameItem.onclick=()=>{menu.remove();_inlineRenameFileItem(item);};
  menu.appendChild(renameItem);

  // Divider + Delete
  const sep=document.createElement('hr');
  sep.style.cssText='border:none;border-top:1px solid var(--border);margin:4px 0;';
  menu.appendChild(sep);
  const delItem=document.createElement('div');
  delItem.textContent=t('delete_title');
  delItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--error,#e94560);';
  delItem.onmouseenter=()=>delItem.style.background='var(--hover)';
  delItem.onmouseleave=()=>delItem.style.background='';
  delItem.onclick=()=>{menu.remove();if(item.type==='dir')deleteWorkspaceDir(item.path,item.name);else deleteWorkspaceFile(item.path,item.name);};
  menu.appendChild(delItem);

  document.body.appendChild(menu);
  const dismiss=()=>{menu.remove();document.removeEventListener('click',dismiss);};
  setTimeout(()=>document.addEventListener('click',dismiss),0);
}

async function _inlineRenameFileItem(item){
  if(!S.session)return;
  const newName=await showPromptDialog({message:t('rename_prompt'),defaultValue:item.name,placeholder:item.name,confirmLabel:t('rename_title')});
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

function renderTray(){
  const tray=$('attachTray');tray.innerHTML='';
  if(!S.pendingFiles.length){tray.classList.remove('has-files');updateSendBtn();return;}
  tray.classList.add('has-files');
  updateSendBtn();
  S.pendingFiles.forEach((f,i)=>{
    const chip=document.createElement('div');chip.className='attach-chip';
    // Image files get a thumbnail preview; other files keep the paperclip chip
    if(_IMAGE_EXTS.test(f.name)){
      const blobUrl=URL.createObjectURL(f);
      chip.className='attach-chip attach-chip--image';
      chip.dataset.blobUrl=blobUrl;
      chip.innerHTML=`<img class="attach-thumb" src="${esc(blobUrl)}" alt="${esc(f.name)}" title="${esc(f.name)}"><button title="${t('remove_title')}">${li('x',12)}</button>`;
    } else if(_SVG_EXTS.test(f.name)){
      const blobUrl=URL.createObjectURL(f);
      chip.className='attach-chip attach-chip--image';
      chip.dataset.blobUrl=blobUrl;
      chip.innerHTML=`<img class="attach-thumb attach-thumb--svg" src="${esc(blobUrl)}" alt="${esc(f.name)}" title="${esc(f.name)}"><button title="${t('remove_title')}">${li('x',12)}</button>`;
    } else if(_AUDIO_EXTS.test(f.name)){
      const blobUrl=URL.createObjectURL(f);
      chip.className='attach-chip attach-chip--audio';
      chip.innerHTML=`<span class="attach-chip-media">🎵 ${esc(f.name)}</span><audio controls preload="metadata" src="${esc(blobUrl)}"></audio><button title="${t('remove_title')}">${li('x',12)}</button>`;
    } else if(_VIDEO_EXTS.test(f.name)){
      const blobUrl=URL.createObjectURL(f);
      chip.className='attach-chip attach-chip--video';
      chip.innerHTML=`<span class="attach-chip-media">🎬 ${esc(f.name)}</span><video controls preload="metadata" src="${esc(blobUrl)}"></video><button title="${t('remove_title')}">${li('x',12)}</button>`;
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
      const isArchive=_ARCHIVE_EXTS.test(f.name);
      const url=new URL(isArchive?'api/upload/extract':'api/upload',location.href).href;
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
