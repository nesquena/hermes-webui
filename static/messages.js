function _markSessionViewed(sid, messageCount) {
  if(typeof _setSessionViewedCount!=='function' || !sid) return;
  const next = Number.isFinite(messageCount) ? Number(messageCount) : 0;
  _setSessionViewedCount(sid, next);
}

function _apiUrl(path) {
  return new URL(path, document.baseURI || location.href).href;
}

// Module-scope dedupe ring buffer for bg_task_complete events. Shared between
// the in-turn STREAMS path (per-turn EventSource inside the chat-stream wirer)
// and the persistent session-scoped path (/api/session/stream), so the
// frontend never double-fires a toast or ack for the same (session_id,
// event_id) regardless of which channel delivered it first. (Option X)
//
// Keyed by `${session_id}|${event_id}` → expiry timestamp (ms since epoch).
// Bounded by a 60-second TTL plus a 256-entry soft cap with insertion-order
// eviction on overflow. Events without `event_id` are ignored by the caller
// (the server contract guarantees `event_id` on every completion emit).
const _BG_TASK_COMPLETE_TTL_MS = 60000;
const _BG_TASK_COMPLETE_CAP = 256;
const _bgTaskCompleteSeenIds = new Map();

function _bgTaskCompleteRingBufferAdd(sid, evt_id) {
  // Missing key → treat as "seen/skip" (return true). The sole caller already
  // guards with `if (!evt_id) return;` before invoking this, so this branch is
  // defensive: returning true (skip) rather than false (proceed) means a
  // future call site that forgets that guard drops the un-keyable event
  // instead of processing a completion with no dedupe key.
  if (!sid || !evt_id) return true;
  const key = sid + '|' + evt_id;
  const now = Date.now();
  // Lazy purge: walk insertion-order; drop any entry whose expiry has passed.
  // Map iteration is insertion-order so this also surfaces the oldest entries
  // first when we need to evict for the soft cap below.
  for (const [k, exp] of _bgTaskCompleteSeenIds) {
    if (exp <= now) {
      _bgTaskCompleteSeenIds.delete(k);
    }
  }
  if (_bgTaskCompleteSeenIds.has(key)) return true;  // duplicate
  _bgTaskCompleteSeenIds.set(key, now + _BG_TASK_COMPLETE_TTL_MS);
  // Soft cap: insertion-order eviction.
  while (_bgTaskCompleteSeenIds.size > _BG_TASK_COMPLETE_CAP) {
    const firstKey = _bgTaskCompleteSeenIds.keys().next().value;
    if (firstKey === undefined) break;
    _bgTaskCompleteSeenIds.delete(firstKey);
  }
  return false;
}

function _isDocumentVisibleAndFocused() {
  if(typeof document!=='undefined' && document.visibilityState && document.visibilityState!=='visible') return false;
  if(typeof document!=='undefined' && typeof document.hasFocus==='function' && !document.hasFocus()) return false;
  return true;
}

function _isSessionCurrentPane(sid) {
  if(!sid || !S.session || S.session.session_id!==sid) return false;
  // During session switching, S.session still points at the previous row until
  // the next metadata request resolves. Do not let a just-finished old stream
  // update the chat pane while the user is moving to another session.
  if(typeof _loadingSessionId!=='undefined' && _loadingSessionId && _loadingSessionId!==sid) return false;
  return true;
}

function _isSessionActivelyViewed(sid) {
  if(!_isSessionCurrentPane(sid)) return false;
  if(!_isDocumentVisibleAndFocused()) return false;
  return true;
}

function _markActiveSessionViewedOnReturn() {
  if(!_isDocumentVisibleAndFocused() || !S.session || !S.session.session_id) return;
  _markSessionViewed(S.session.session_id, S.session.message_count || (S.messages&&S.messages.length) || 0);
  if(typeof _clearSessionCompletionUnread==='function') _clearSessionCompletionUnread(S.session.session_id);
  if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
}

function _chatPayloadModel(){
  return S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'';
}

function _chatPayloadModelProvider(model){
  if(typeof _modelProviderForSend==='function') return _modelProviderForSend(model);
  if(S.session&&S.session.model_provider) return S.session.model_provider||null;
  return null;
}

function _chatPayloadModelState(){
  // Source-compat invariant: the starting precedence is still
  // model:S.session.model||$('modelSelect').value and
  // model_provider:S.session.model_provider||null. The helper only fills a
  // missing provider when it belongs to the same outgoing model.
  const model=_chatPayloadModel();
  return {model,model_provider:_chatPayloadModelProvider(model)};
}

function _deferStreamErrorIfOffline(){
  if(typeof isOfflineBannerVisible==='function' && isOfflineBannerVisible()){
    setComposerStatus(t('offline_stream_waiting'));
    return true;
  }
  if(typeof showOfflineBanner==='function' && navigator.onLine===false){
    showOfflineBanner('browser');
    setComposerStatus(t('offline_stream_waiting'));
    return true;
  }
  return false;
}

document.addEventListener('visibilitychange', _markActiveSessionViewedOnReturn);
window.addEventListener('focus', _markActiveSessionViewedOnReturn);

// Delegated click handler for the interim-progress-note collapse toggle (#2403).
// Delegation (not a per-element listener) is required because the live turn's
// DOM is snapshotted/restored via outerHTML/innerHTML on session switch
// (snapshotLiveTurnHtmlForSession / restoreLiveTurnHtmlForSession in ui.js),
// which strips element listeners. A document-level handler survives the
// restore so a restored toggle stays interactive and collapsed notes never
// become permanently unreachable. State lives in the DOM (presence of
// .interim-collapsed + data-threshold on the toggle), so the handler is
// stateless and works on freshly-created and restored toggles alike.
function _interimCollapseDelegatedClick(e){
  const toggle=e.target&&e.target.closest?e.target.closest('.interim-collapse-toggle'):null;
  if(!toggle) return;
  const blocks=toggle.parentElement;
  if(!blocks) return;
  const threshold=parseInt(toggle.dataset.threshold,10)||3;
  const hidden=blocks.querySelectorAll('.interim-collapsed');
  if(hidden.length){
    hidden.forEach(el=>el.classList.remove('interim-collapsed'));
    toggle.dataset.expanded='1';
    toggle.textContent='Collapse';
  } else {
    const all=Array.from(blocks.querySelectorAll('[data-interim="1"]'));
    const rehide=all.slice(0,all.length-threshold);
    rehide.forEach(el=>el.classList.add('interim-collapsed'));
    toggle.dataset.expanded='';
    toggle.textContent='Show '+rehide.length+' earlier update'+(rehide.length===1?'':'s');
  }
}
document.addEventListener('click', _interimCollapseDelegatedClick);

// TTS: pause speech synthesis when user focuses the composer (#499)
const _msgEl=document.getElementById('msg');
if(_msgEl) _msgEl.addEventListener('focus', ()=>{ if('speechSynthesis' in window && speechSynthesis.speaking) speechSynthesis.pause(); });
if(_msgEl) _msgEl.addEventListener('blur', ()=>{ if('speechSynthesis' in window && speechSynthesis.paused) speechSynthesis.resume(); });

let _selectedTextReplyBtn=null;
let _selectedTextReplyText='';
let _pendingSelections=[];  // [{id, name, text}] — named context blocks
let _selectionIdCounter=0;
// #4380: expose a pending-selection predicate so the composer's primary-action
// content check (_composerHasContent in ui.js) treats selection-only replies as
// sendable content even though they no longer live in the textarea.
if(typeof window!=='undefined'){
  window._hasPendingSelections=function(){return _pendingSelections.length>0;};
}
let _selectedTextReplyRaf=0;
const _persistentStateToastSeen=new Set();
const _thinkPairs=[
  {open:'<think>',close:'</think>'},
  {open:'<|channel>thought\n',close:'<channel|>'},
  {open:'<|turn|>thinking\n',close:'<turn|>'}
];

function _thinkingFenceMarkerAt(text, index){
  // A fenced code block opener may be indented up to 3 spaces in Markdown
  // (4+ spaces is an indented code block, handled separately). Only treat the
  // marker as a fence when it sits at a line start after optional 1-3 spaces.
  if(index>0&&text[index-1]!=='\n'){
    let back=index-1, spaces=0;
    while(back>=0&&text[back]===' '&&spaces<3){back--;spaces++;}
    if(!(back<0||text[back]==='\n')) return '';
  }
  if(text.startsWith('```',index)) return '```';
  if(text.startsWith('~~~',index)) return '~~~';
  return '';
}

function _nextThinkingOpener(text, start){
  // Index of the earliest complete thinking opener at/after `start`, or -1.
  // Cheap indexOf per opener — lets the scanner bulk-skip plain trailing content
  // instead of walking it char-by-char (#3633 Codex per-token perf catch).
  let best=-1;
  for(const p of _thinkPairs){
    const i=text.indexOf(p.open,start);
    if(i!==-1&&(best===-1||i<best)) best=i;
  }
  return best;
}

function _textTailIsPartialOpener(text){
  // True when the END of text is a non-empty proper prefix of some opener
  // (e.g. "<thi" for "<think>"). Decides whether a streaming tail might be a
  // forming block worth code-aware handling.
  for(const p of _thinkPairs){
    const m=Math.min(p.open.length-1,text.length);
    for(let n=m;n>0;n--){ if(p.open.startsWith(text.slice(text.length-n))) return true; }
  }
  return false;
}

function _lineIsIndentedCode(text, lineStart){
  // True when the line beginning at lineStart is a markdown indented code block
  // line (>=4 leading spaces or a leading tab, and not blank). lineStart must be
  // the first char of the line. Only inspects the line's leading chars, not the
  // whole document (the per-character variant was O(n^2) on long no-newline
  // content — #3633 Codex perf catch).
  if(lineStart>=text.length) return false;
  if(text[lineStart]==='\t'||text.startsWith('    ',lineStart)){
    let nl=text.indexOf('\n',lineStart);
    if(nl===-1) nl=text.length;
    return text.slice(lineStart,nl).trim()!=='';
  }
  return false;
}

function _mergeInlineThinkingReasoning(existingReasoning, extractedParts){
  let out=String(existingReasoning||'').trim();
  (Array.isArray(extractedParts)?extractedParts:[]).forEach(function(part){
    const item=String(part||'').trim();
    if(!item) return;
    if(!out){out=item;return;}
    if(out===item||out.split('\n\n').some(function(existing){return existing.trim()===item;})) return;
    out += '\n\n' + item;
  });
  return out;
}

function _extractInlineThinkingFromContent(rawContent, existingReasoning, options){
  // Code-aware extraction (must mirror api/streaming.py
  // _extract_inline_thinking_from_content): thinking tags inside a triple-fence,
  // an inline single-backtick code span, or an indented code block are LEFT
  // VISIBLE. options.streaming gates partial/unclosed handling — only during a
  // live stream does an unmatched open tag mean "still thinking"; on the
  // reload/render path an unclosed tag stays visible content (#3633 Codex catch).
  const streaming=!!(options&&options.streaming);
  const text=String(rawContent||'');
  if(!text){
    const reasoning=String(existingReasoning||'').trim();
    return {reasoning,content:text,thinkingText:reasoning,displayText:text,inThinking:false};
  }
  // Fast path (#3633 Codex perf catch — _parseStreamState / syncInflightAssistantMessage
  // call this on the FULL accumulator on every streamed token, so the common no-tag
  // case must not do the O(length) char walk per call). If no complete opener is
  // present AND — when streaming — the tail is not a prefix of an opener, there is
  // nothing to extract: return the text unchanged (two cheap substring scans).
  if(!_thinkPairs.some(p=>text.indexOf(p.open)!==-1)){
    let tailIsPartialOpener=false;
    if(streaming){
      for(const p of _thinkPairs){
        const maxPrefix=Math.min(p.open.length-1,text.length);
        for(let n=maxPrefix;n>0;n--){
          if(p.open.startsWith(text.slice(text.length-n))){tailIsPartialOpener=true;break;}
        }
        if(tailIsPartialOpener) break;
      }
    }
    if(!tailIsPartialOpener){
      const reasoning=String(existingReasoning||'').trim();
      return {reasoning,content:text,thinkingText:reasoning,displayText:text,inThinking:false};
    }
  }
  const visible=[];
  const extracted=[];
  let cursor=0;
  let index=0;
  let fence='';
  let inBacktick=false;
  let inThinking=false;
  // Incremental O(1)-per-iteration line state + seen-nonspace flag (the previous
  // per-character line scan + slice(0,index).trim() were O(n^2) on long
  // no-newline content — #3633 Codex perf catch).
  let lineIsIndentedCode=_lineIsIndentedCode(text,0);
  let seenNonspace=false;
  // Only lstrip the final content when a LEADING thinking block/prefix was
  // removed — a reply that legitimately starts with indented code / whitespace
  // and has no leading thinking wrapper keeps its leading whitespace (#3633
  // Codex catch).
  let leadingRemoved=false;
  // Index of the next complete opener at/after `index` — lets the scanner bulk-skip
  // plain trailing content instead of walking it char-by-char every streamed token
  // (#3633 Codex per-token perf catch).
  let nextOpener=_nextThinkingOpener(text,0);
  while(index<text.length){
    if(nextOpener===-1||index>nextOpener) nextOpener=_nextThinkingOpener(text,index);
    if(nextOpener===-1){
      // No further COMPLETE opener ahead — remaining tail is plain and is
      // appended in one slice, EXCEPT during streaming when the tail is a prefix
      // of an opener ("...<thi"): it may be a forming block and must be
      // suppressed, but ONLY if outside code context (a partial opener inside
      // inline-backtick / fenced / indented code stays visible — master parity).
      // Code state needs the char walk, so fall through in that case (bounded —
      // a partial tail is a transient single token) instead of bulk-skipping.
      if(streaming&&_textTailIsPartialOpener(text)){
        // fall through to the code-aware char walk for the tail
      } else {
        break;
      }
    }
    const ch=text[index];
    if(index>0&&text[index-1]==='\n') lineIsIndentedCode=_lineIsIndentedCode(text,index);
    const marker=_thinkingFenceMarkerAt(text,index);
    if(marker) fence=(fence===marker)?'':(fence||marker);
    if(!fence&&!marker&&ch==='`') inBacktick=!inBacktick;
    const inCode=!!fence||inBacktick||lineIsIndentedCode;
    if(!inCode){
      let pair=null;
      for(const candidate of _thinkPairs){
        if(text.startsWith(candidate.open,index)){pair=candidate;break;}
      }
      if(pair){
        const closeIndex=text.indexOf(pair.close,index+pair.open.length);
        if(closeIndex===-1){
          // Unclosed open tag. A LEADING unclosed block (nothing visible before
          // it) is a genuine thinking trace cut off mid-thought → reasoning
          // (master #3455 leading-only intent + live "still thinking"). An
          // unclosed tag AFTER visible content on the reload/render path is
          // almost always a literal typed tag — leave it (and following prose)
          // visible so nothing is silently truncated (#3633 Codex catch).
          const leading=!seenNonspace;
          if(!streaming&&!leading) break;
          if(leading) leadingRemoved=true;
          visible.push(text.slice(cursor,index));
          const partial=text.slice(index+pair.open.length);
          if(partial) extracted.push(partial);
          inThinking=true;
          cursor=text.length;
          index=text.length;
          break;
        }
        visible.push(text.slice(cursor,index));
        extracted.push(text.slice(index+pair.open.length,closeIndex));
        if(!seenNonspace) leadingRemoved=true;
        seenNonspace=true;
        index=closeIndex+pair.close.length;
        cursor=index;
        continue;
      }
      if(streaming){
        let matchedPartial=false;
        for(const candidate of _thinkPairs){
          const rest=text.slice(index);
          if(rest.length<candidate.open.length&&candidate.open.startsWith(rest)){
            if(!seenNonspace) leadingRemoved=true;
            visible.push(text.slice(cursor,index));
            inThinking=true;
            cursor=text.length;
            index=text.length;
            matchedPartial=true;
            break;
          }
        }
        if(matchedPartial||index>=text.length) break;
      }
    }
    if(ch.trim()!=='') seenNonspace=true;
    index++;
  }
  if(cursor<text.length) visible.push(text.slice(cursor));
  const content=leadingRemoved?visible.join('').replace(/^\s+/,''):visible.join('');
  const reasoning=_mergeInlineThinkingReasoning(existingReasoning,extracted);
  return {reasoning,content,thinkingText:reasoning,displayText:content,inThinking};
}

if(typeof window!=='undefined'){
  window._extractInlineThinkingFromContentForRender=function(rawContent, existingReasoning){
    return _extractInlineThinkingFromContent(rawContent, existingReasoning, {streaming:false});
  };
}

function enhanceMarkdownTables(root){
  if(!root||!root.querySelectorAll) return;
  const scope=root;
  const tables=scope.querySelectorAll('.msg-body table:not([data-markdown-table-enhanced])');
  const sortLabel=typeof t==='function'?t('markdown_table_sort_column'):'Sort column';
  const filterLabel=typeof t==='function'?t('markdown_table_filter'):'Filter table';
  tables.forEach((table)=>{
    if(table.closest('.csv-table-wrap')) return;
    const headRows=table.tHead?Array.from(table.tHead.rows):[];
    const body=table.tBodies&&table.tBodies.length?table.tBodies[0]:table;
    const bodyRows=Array.from(body.rows||[]).filter((row)=>row.parentElement===body);
    const headerRow=headRows[0]||table.querySelector('tr');
    if(!headerRow||!bodyRows.length) return;
    table.setAttribute('data-markdown-table-enhanced','1');
    bodyRows.forEach((row,idx)=>{ row.dataset.markdownTableOriginalIndex=String(idx); });

    if(bodyRows.length>=4&&table.parentElement){
      const filter=document.createElement('input');
      filter.type='search';
      filter.className='markdown-table-filter';
      filter.placeholder=filterLabel;
      filter.setAttribute('aria-label',filterLabel);
      filter.autocomplete='off';
      filter.spellcheck=false;
      filter.addEventListener('input',()=>{
        const query=_markdownTableText(filter.value).toLowerCase();
        bodyRows.forEach((row)=>{
          row.hidden=!!query&&!_markdownTableText(row.textContent).toLowerCase().includes(query);
        });
      });
      table.parentElement.insertBefore(filter,table);
    }

    Array.from(headerRow.cells||[]).forEach((cell,colIdx)=>{
      const button=document.createElement('button');
      button.type='button';
      button.className='markdown-table-sort';
      const columnName=_markdownTableText(cell.textContent)||String(colIdx+1);
      const columnSortLabel=`${sortLabel}: ${columnName}`;
      button.setAttribute('aria-label',columnSortLabel);
      button.title=columnSortLabel;
      cell.setAttribute('aria-sort','none');
      const label=document.createElement('span');
      label.className='markdown-table-sort-label';
      while(cell.firstChild) label.appendChild(cell.firstChild);
      const indicator=document.createElement('span');
      indicator.className='markdown-table-sort-indicator';
      indicator.setAttribute('aria-hidden','true');
      button.appendChild(label);
      button.appendChild(indicator);
      button.addEventListener('click',()=>{
        const nextDir=table.dataset.markdownTableSortCol===String(colIdx)&&table.dataset.markdownTableSortDir==='asc'?'desc':'asc';
        table.dataset.markdownTableSortCol=String(colIdx);
        table.dataset.markdownTableSortDir=nextDir;
        Array.from(headerRow.cells||[]).forEach((other)=>{
          other.setAttribute('aria-sort','none');
        });
        cell.setAttribute('aria-sort',nextDir==='asc'?'ascending':'descending');
        const rows=Array.from(body.rows||[]).filter((row)=>row.parentElement===body);
        rows.sort((a,b)=>{
          const av=_markdownTableCellText(a.cells[colIdx]);
          const bv=_markdownTableCellText(b.cells[colIdx]);
          const cmp=av.localeCompare(bv,undefined,{numeric:true,sensitivity:'base'});
          if(cmp!==0) return nextDir==='asc'?cmp:-cmp;
          const ai=Number(a.dataset.markdownTableOriginalIndex||0);
          const bi=Number(b.dataset.markdownTableOriginalIndex||0);
          return ai-bi;
        });
        rows.forEach((row)=>body.appendChild(row));
      });
      cell.appendChild(button);
    });
  });
}

function _markdownTableText(value){
  return String(value||'').replace(/\s+/g,' ').trim();
}

function _markdownTableCellText(cell){
  return _markdownTableText(cell?cell.textContent:'');
}

window.enhanceMarkdownTables=enhanceMarkdownTables;

(function _wireMarkdownTableEnhancer(){
  if(typeof window==='undefined'||typeof window.renderMessages!=='function'||window.renderMessages._markdownTablesEnhanced) return;
  const baseRenderMessages=window.renderMessages;
  window.renderMessages=function(...args){
    const result=baseRenderMessages.apply(this,args);
    const inner=typeof $==='function'?$('msgInner'):document.getElementById('msgInner');
    enhanceMarkdownTables(inner);
    return result;
  };
  window.renderMessages._markdownTablesEnhanced=true;
})();

function _persistentToastText(value){
  if(value===null||value===undefined)return '';
  if(typeof value==='string')return value;
  try{return JSON.stringify(value);}catch(_){return String(value||'');}
}

function _persistentToastToolName(tool){
  return String(tool&&tool.name||'').trim();
}

function _persistentToastArgs(tool){
  const args=tool&&tool.args;
  return args&&typeof args==='object'?args:{};
}

function _persistentToastPreview(tool){
  return [
    _persistentToastText(tool&&tool.preview),
    _persistentToastText(tool&&tool.snippet),
  ].filter(Boolean).join('\n');
}

function _persistentToastHasWriteIntent(name, text){
  const nameWords=String(name||'').replace(/_/g,' ');
  const haystack=`${nameWords}\n${text}`.toLowerCase();
  if(/\b(read|list|view|search|lookup|get|fetch|load|usage|toggle|delete|remove)\b/.test(nameWords))return false;
  if(/\b(no|not|nothing)\s+(?:was\s+)?(?:saved|updated|created|written|stored|changed)\b/.test(haystack))return false;
  if(/\b(?:unchanged|skipped|dry[- ]run|failed|error)\b/.test(haystack))return false;
  return /\b(save|saved|write|wrote|written|update|updated|create|created|store|stored|persist|persisted|remember|remembered)\b/.test(haystack);
}

function _persistentToastSkillName(tool){
  const args=_persistentToastArgs(tool);
  const raw=args.name||args.skill_name||args.skill||args.title||'';
  const direct=String(raw||'').trim();
  if(direct)return direct;
  const text=_persistentToastPreview(tool);
  const match=text.match(/\bskill(?:\s+updated|\s+created|\s+saved)?\s*[:=]\s*["'`]?([A-Za-z0-9_.-]{2,80})/i);
  return match?match[1]:'';
}

function _maybeNotifyPersistentStateSaved(tool){
  if(!tool||tool.is_error||typeof showToast!=='function')return;
  const name=_persistentToastToolName(tool);
  if(!name)return;
  const nameKey=name.toLowerCase().replace(/[^a-z0-9]+/g,'_');
  const preview=_persistentToastPreview(tool);
  const argsText=_persistentToastText(_persistentToastArgs(tool));
  const text=`${preview}\n${argsText}`;
  if(!_persistentToastHasWriteIntent(nameKey, text))return;

  const nameWords=nameKey.replace(/_/g,' ');
  const isSkill=/\bskills?\b/.test(nameWords);
  const isMemory=/\b(memory|memories|remember|profile)\b/.test(nameWords);
  if(!isSkill&&!isMemory)return;
  const skillName=isSkill?_persistentToastSkillName(tool):'';
  if(isSkill&&!skillName)return;
  _showPersistentStateToast(isSkill?'skill':'memory', skillName, {
    created: isSkill&&/\b(create|created|new)\b/.test(`${nameKey}\n${preview}`.toLowerCase()),
  });
}

function _showPersistentStateToast(kind, name, options){
  if(typeof showToast!=='function')return;
  const normalizedKind=String(kind||'').toLowerCase();
  if(normalizedKind!=='skill'&&normalizedKind!=='memory')return;
  const itemName=String(name||'').trim();
  const dedupeKey=[
    S&&S.session&&S.session.session_id||'',
    normalizedKind,
    itemName||'memory',
  ].join(':');
  if(_persistentStateToastSeen.has(dedupeKey))return;
  _persistentStateToastSeen.add(dedupeKey);
  if(_persistentStateToastSeen.size>200){
    const first=_persistentStateToastSeen.values().next().value;
    _persistentStateToastSeen.delete(first);
  }

  if(normalizedKind==='skill'){
    const base=options&&options.created?t('skill_created'):t('skill_updated');
    showToast(itemName?`${base}: ${itemName}`:base,4200,'success');
    return;
  }
  showToast(t('memory_saved'),3600,'success');
}

function _selectedTextReplyT(key, fallback){
  try{
    const val=(typeof t==='function')?t(key):'';
    return val&&val!==key?val:fallback;
  }catch(_err){
    return fallback;
  }
}

function _selectedTextReplyRoot(){
  if(typeof $==='function') return $('messages')||$('msgInner');
  return document.getElementById('messages')||document.getElementById('msgInner');
}

function _selectedTextReplyNodeInChat(node, root){
  if(!node||!root)return false;
  const el=node.nodeType===Node.ELEMENT_NODE?node:node.parentElement;
  return !!(el&&root.contains(el));
}

function _selectedTextReplySelection(){
  if(!window.getSelection)return null;
  const selection=window.getSelection();
  if(!selection||selection.isCollapsed||!selection.rangeCount)return null;
  const root=_selectedTextReplyRoot();
  if(!root)return null;
  const range=selection.getRangeAt(0);
  if(!_selectedTextReplyNodeInChat(range.startContainer, root)||!_selectedTextReplyNodeInChat(range.endContainer, root))return null;
  const text=selection.toString().replace(/\u00a0/g,' ').trim();
  if(!text)return null;
  const rect=range.getBoundingClientRect();
  if(!rect||(!rect.width&&!rect.height))return null;
  return {text, rect};
}

function _formatSelectedTextReplyQuote(text){
  const normalized=String(text||'').replace(/\r\n?/g,'\n').replace(/\n{3,}/g,'\n\n').trim();
  if(!normalized)return '';
  return `<!-- hermes-selected-context -->\n${normalized.split('\n').map(line=>`> ${line}`).join('\n')}`;
}

function insertSavedPromptIntoComposer(text){
  const composer=(typeof $==='function'&&$('msg'))||document.getElementById('msg');
  if(!composer||!text)return;
  const current=String(composer.value||'');
  composer.value=current.trim()?`${current.replace(/\s+$/,'')}\n\n${text}\n\n`:`${text}\n\n`;
  composer.focus();
  try{composer.setSelectionRange(composer.value.length, composer.value.length);}catch(_e){}
  composer.dispatchEvent(new Event('input',{bubbles:true}));
  if(typeof autoResize==='function') autoResize();
}

let _savedPromptsCache=null;

async function _loadSavedPrompts(){
  try{
    const data=await api('/api/prompts');
    _savedPromptsCache=Array.isArray(data&&data.prompts)?data.prompts:[];
  }catch(_e){_savedPromptsCache=[];}
  return _savedPromptsCache;
}

async function toggleSavedPromptsPopup(){
  const popup=(typeof $==='function'&&$('savedPromptsPopup'))||document.getElementById('savedPromptsPopup');
  const btn=(typeof $==='function'&&$('btnSavedPrompts'))||document.getElementById('btnSavedPrompts');
  if(!popup)return;
  if(popup.style.display!=='none'){
    popup.style.display='none';
    if(btn)btn.setAttribute('aria-expanded','false');
    return;
  }
  popup.innerHTML='<div class="saved-prompts-loading">Loading…</div>';
  popup.style.display='flex';
  if(btn)btn.setAttribute('aria-expanded','true');
  const prompts=await _loadSavedPrompts();
  popup.innerHTML='';
  if(!prompts.length){
    const empty=document.createElement('div');
    empty.className='saved-prompts-empty';
    empty.textContent=(typeof t==='function'&&t('saved_prompts_empty'))||'No saved prompts yet.';
    popup.appendChild(empty);
  }else{
    for(const p of prompts){
      const row=document.createElement('div');
      row.className='saved-prompt-row';
      row.setAttribute('role','menuitem');
      const label=document.createElement('span');
      label.className='saved-prompt-label';
      label.textContent=p.label||p.text;
      label.title=p.text;
      row.onclick=()=>{
        insertSavedPromptIntoComposer(p.text);
        popup.style.display='none';
        if(btn)btn.setAttribute('aria-expanded','false');
      };
      const del=document.createElement('button');
      del.className='saved-prompt-delete';
      del.type='button';
      del.title=(typeof t==='function'&&t('saved_prompts_delete'))||'Delete';
      del.innerHTML='<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
      del.onclick=async(e)=>{
        e.stopPropagation();
        try{await api('/api/prompts',{method:'DELETE',body:JSON.stringify({id:p.id})});}catch(_e){}
        _savedPromptsCache=null;
        await toggleSavedPromptsPopup();
        await toggleSavedPromptsPopup();
      };
      row.appendChild(label);
      row.appendChild(del);
      popup.appendChild(row);
    }
  }
  const addRow=document.createElement('div');
  addRow.className='saved-prompt-add-row';
  const saveBtn=document.createElement('button');
  saveBtn.type='button';
  saveBtn.className='saved-prompt-save-btn';
  saveBtn.textContent=(typeof t==='function'&&t('saved_prompts_save_current'))||'Save current input';
  saveBtn.onclick=async()=>{
    const msgEl=(typeof $==='function'&&$('msg'))||document.getElementById('msg');
    const text=(msgEl&&msgEl.value||'').trim();
    if(!text){
      if(typeof showToast==='function') showToast((typeof t==='function'&&t('saved_prompts_empty_input'))||'Type a prompt first',2000,'error');
      return;
    }
    try{await api('/api/prompts',{method:'POST',body:JSON.stringify({text})});}catch(_e){
      if(typeof showToast==='function') showToast(_e&&_e.message||'Failed to save prompt',2000,'error');
      return;
    }
    _savedPromptsCache=null;
    popup.style.display='none';
    if(btn)btn.setAttribute('aria-expanded','false');
    if(typeof showToast==='function') showToast((typeof t==='function'&&t('saved_prompts_saved'))||'Prompt saved',1600);
  };
  addRow.appendChild(saveBtn);
  popup.appendChild(addRow);
}

document.addEventListener('click',(e)=>{
  const popup=(typeof $==='function'&&$('savedPromptsPopup'))||document.getElementById('savedPromptsPopup');
  const btn=(typeof $==='function'&&$('btnSavedPrompts'))||document.getElementById('btnSavedPrompts');
  if(!popup||popup.style.display==='none')return;
  if(!popup.contains(e.target)&&e.target!==btn&&!(btn&&btn.contains(e.target))){
    popup.style.display='none';
    if(btn)btn.setAttribute('aria-expanded','false');
  }
},{capture:false});
function _addNamedContextBlock(text){
  const id='ctx-'+(++_selectionIdCounter);
  const name=(_selectedTextReplyT('context_block_name_default','Context'))+' '+_selectionIdCounter;
  _pendingSelections.push({id, name, text});
  _renderSelectionChips();
  return id;
}

function _removeNamedContextBlock(id){
  _pendingSelections=_pendingSelections.filter(s=>s.id!==id);
  _renderSelectionChips();
}

function _clearPendingSelections(){
  if(!_pendingSelections.length)return false;
  _pendingSelections=[];
  _renderSelectionChips();
  return true;
}
if(typeof window!=='undefined') window._clearPendingSelections=_clearPendingSelections;

function _selectedContextPreview(text){
  const normalized=String(text||'').replace(/\r\n?/g,'\n').replace(/\n{3,}/g,'\n\n').trim();
  if(!normalized)return '';
  const max=360;
  return normalized.length>max?normalized.slice(0,max).trimEnd()+'…':normalized;
}

function _renderSelectionChips(){
  const wrap=document.getElementById('composerSelectionChips');
  if(!wrap)return;
  wrap.innerHTML='';
  wrap.hidden=!_pendingSelections.length;
  _pendingSelections.forEach(s=>{
    const card=document.createElement('article');
    card.className='selection-context-card';
    card.dataset.selectionId=s.id;
    card.setAttribute('aria-label', s.name);

    const accent=document.createElement('div');
    accent.className='selection-context-accent';
    accent.setAttribute('aria-hidden','true');

    const body=document.createElement('div');
    body.className='selection-context-body';

    const header=document.createElement('div');
    header.className='selection-context-header';

    const name=document.createElement('button');
    name.type='button';
    name.className='selection-context-name selection-chip-name';
    name.textContent=s.name;
    name.title=_selectedTextReplyT('context_block_rename_hint','Click or press Enter to rename');
    name.setAttribute('aria-label', `${_selectedTextReplyT('context_block_rename_aria','Rename context block')}: ${s.name}`);
    name.addEventListener('click',()=>_editSelectionChipName(s.id,card));
    name.addEventListener('dblclick',()=>_editSelectionChipName(s.id,card));
    name.addEventListener('keydown',e=>{
      if(e.key==='Enter'||e.key===' '||e.key==='F2'){
        e.preventDefault();
        _editSelectionChipName(s.id,card);
      }
    });

    const remove=document.createElement('button');
    remove.type='button';
    remove.className='selection-context-remove selection-chip-remove';
    remove.setAttribute('aria-label', `${_selectedTextReplyT('context_block_remove','Remove context block')}: ${s.name}`);
    remove.innerHTML='&#x2715;';
    remove.addEventListener('click',()=>_removeNamedContextBlock(s.id));

    const quote=document.createElement('blockquote');
    quote.className='selection-context-quote';
    quote.textContent=_selectedContextPreview(s.text);
    quote.title=String(s.text||'');

    header.appendChild(name);
    header.appendChild(remove);
    body.appendChild(header);
    body.appendChild(quote);
    card.appendChild(accent);
    card.appendChild(body);
    wrap.appendChild(card);
  });
  // #4380: pending selection cards are content the primary Send button must
  // recognize (they were moved out of the textarea into _pendingSelections),
  // so refresh the button's enabled/disabled state whenever the set changes —
  // otherwise a selection-only reply can't be sent via click/tap/mobile.
  if(typeof updateSendBtn==='function') updateSendBtn();
}

function _editSelectionChipName(id,chip){
  const s=_pendingSelections.find(x=>x.id===id);
  if(!s)return;
  const nameEl=chip.querySelector('.selection-chip-name');
  if(!nameEl)return;
  if(chip.querySelector('.selection-chip-edit'))return;
  const inp=document.createElement('input');
  inp.type='text';inp.value=s.name;inp.className='selection-chip-edit';
  inp.maxLength=120;
  inp.setAttribute('aria-label', `${_selectedTextReplyT('context_block_rename_aria','Rename context block')}: ${s.name}`);
  inp.title=_selectedTextReplyT('context_block_rename_hint','Click or press Enter to rename');
  nameEl.replaceWith(inp);
  inp.focus();inp.select();
  let done=false;
  const restoreFocus=()=>{
    window.requestAnimationFrame(()=>{
      const safeId=window.CSS&&CSS.escape?CSS.escape(id):String(id).replace(/"/g,'\\"');
      const next=document.querySelector(`[data-selection-id="${safeId}"] .selection-chip-name`);
      if(next&&typeof next.focus==='function')next.focus({preventScroll:true});
    });
  };
  const commit=()=>{ if(done)return; done=true; s.name=(inp.value.trim()||s.name).slice(0,120); _renderSelectionChips(); restoreFocus(); };
  const cancel=()=>{ if(done)return; done=true; _renderSelectionChips(); restoreFocus(); };
  inp.addEventListener('blur',commit);
  inp.addEventListener('keydown',e=>{ if(e.key==='Enter'){e.preventDefault();commit();} if(e.key==='Escape'){cancel();} });
}

function _composerTextWithPendingSelections(){
  const composer=(typeof $==='function'&&$('msg'))||document.getElementById('msg');
  const current=String(composer&&composer.value||'');
  if(!_pendingSelections.length)return current;
  const blocks=_pendingSelections.map(s=>`**${s.name}:**\n${_formatSelectedTextReplyQuote(s.text)}`).join('\n\n');
  return current.trim()?`${current.replace(/\s+$/,'')}\n\n${blocks}\n\n`:`${blocks}\n\n`;
}

function _clearComposerAfterQueuedSelectionSend(){
  const composer=(typeof $==='function'&&$('msg'))||document.getElementById('msg');
  if(composer)composer.value='';
  _clearPendingSelections();
  if(typeof autoResize==='function') autoResize();
}

function _flushSelectionBlocksToComposer(){
  if(!_pendingSelections.length)return;
  const composer=(typeof $==='function'&&$('msg'))||document.getElementById('msg');
  if(!composer)return;
  composer.value=_composerTextWithPendingSelections();
  _clearPendingSelections();
  composer.focus();
  try{ composer.setSelectionRange(composer.value.length, composer.value.length); }catch(_e){}
  composer.dispatchEvent(new Event('input',{bubbles:true}));
  if(typeof autoResize==='function') autoResize();
}

function _selectedTextReplyButton(){
  if(_selectedTextReplyBtn)return _selectedTextReplyBtn;
  const btn=document.createElement('button');
  btn.type='button';
  btn.id='selectedTextReplyBtn';
  btn.className='selected-text-reply-btn';
  btn.setAttribute('data-i18n', 'selected_text_reply');
  btn.setAttribute('data-i18n-title', 'selected_text_reply_title');
  btn.setAttribute('data-i18n-aria-label', 'selected_text_reply_title');
  btn.textContent=_selectedTextReplyT('selected_text_reply', 'Reply with selection');
  btn.title=_selectedTextReplyT('selected_text_reply_title', 'Append selected chat text as quoted context');
  btn.setAttribute('aria-label', btn.title);
  btn.addEventListener('mousedown', e=>e.preventDefault());
  btn.addEventListener('click', e=>{
    e.preventDefault();
    if(_selectedTextReplyText){
      _addNamedContextBlock(_selectedTextReplyText);
      _hideSelectedTextReplyButton();
      const selection=window.getSelection&&window.getSelection();
      if(selection&&selection.removeAllRanges)selection.removeAllRanges();
    }
  });
  document.body.appendChild(btn);
  if(typeof applyLocaleToDOM==='function') applyLocaleToDOM();
  _selectedTextReplyBtn=btn;
  return btn;
}

function _hideSelectedTextReplyButton(){
  _selectedTextReplyText='';
  if(_selectedTextReplyBtn)_selectedTextReplyBtn.classList.remove('visible');
}

function _positionSelectedTextReplyButton(info){
  const btn=_selectedTextReplyButton();
  _selectedTextReplyText=info.text;
  btn.classList.add('visible');
  const gap=8;
  const btnRect=btn.getBoundingClientRect();
  const width=btnRect.width||150;
  const height=btnRect.height||32;
  const left=Math.min(Math.max(gap, info.rect.left+(info.rect.width/2)-(width/2)), Math.max(gap, window.innerWidth-width-gap));
  const top=Math.max(gap, info.rect.top-height-gap);
  btn.style.left=`${left}px`;
  btn.style.top=`${top}px`;
}

function _updateSelectedTextReplyButton(){
  if(_selectedTextReplyRaf)return;
  _selectedTextReplyRaf=window.requestAnimationFrame(()=>{
    _selectedTextReplyRaf=0;
    const info=_selectedTextReplySelection();
    if(!info){
      _hideSelectedTextReplyButton();
      return;
    }
    _positionSelectedTextReplyButton(info);
  });
}

if(typeof document!=='undefined'){
  document.addEventListener('selectionchange', _updateSelectedTextReplyButton);
  document.addEventListener('mouseup', e=>{
    if(e.target&&e.target.closest&&e.target.closest('.selected-text-reply-btn'))return;
    _updateSelectedTextReplyButton();
  });
  document.addEventListener('keyup', e=>{
    if(e.key&&/Arrow|Shift|Control|Meta|Alt/.test(e.key))_updateSelectedTextReplyButton();
  });
  window.addEventListener('resize', _hideSelectedTextReplyButton);
}

// Guard against concurrent send() calls.  Without this, two rapid sends
// (e.g. queue drain + user click) can both pass the S.busy check because
// setBusy(true) is only called after the first await inside send().
let _sendInProgress = false;
let _sendInProgressSid = null;  // session_id of the in-flight send
const _sessionTitleProvisionalBySid = new Map();
// Agent commands that are safe to execute directly in the WebUI even though
// their canonical command is registered on the backend (for example
// /reload-mcp). Keep this intentionally narrow and include underscore variants
// observed by users so typing either form still routes through executeAgentCommand.
const _AGENT_COMMANDS_RUN_ON_WEBUI = new Set(['reload-mcp', 'reload_mcp', 'reload-skills', 'reload_skills', 'codex-runtime', 'codex_runtime', 'credits']);

function _clearStaleBusyStateBeforeSend({compressionRunning=false}={}){
  if(!S||!S.busy||compressionRunning) return false;
  const session=S.session||{};
  const sid=session.session_id||'';
  const hasRuntimeConfirmation=Boolean(
    S.activeStreamId||
    session.active_stream_id||
    session.pending_user_message||
    session.pending_started_at
  );
  if(hasRuntimeConfirmation) return false;
  if(typeof INFLIGHT==='object'&&INFLIGHT&&sid&&INFLIGHT[sid]){
    delete INFLIGHT[sid];
    if(typeof clearInflightState==='function') clearInflightState(sid);
  }
  S.activeStreamId=null;
  if(session) session.active_stream_id=null;
  if(typeof setBusy==='function') setBusy(false);
  else S.busy=false;
  if(typeof setComposerStatus==='function') setComposerStatus('');
  if(typeof setStatus==='function') setStatus('');
  if(typeof updateSendBtn==='function') updateSendBtn();
  if(sid&&typeof clearOptimisticSessionStreaming==='function') clearOptimisticSessionStreaming(sid);
  return true;
}

function _runOptionalPreStartUiStep(label, fn){
  try{
    return typeof fn==='function'?fn():undefined;
  }catch(e){
    const message=e&&e.message?e.message:String(e||'unknown error');
    try{console.warn('[webui] optional pre-start UI step failed', label, message);}catch(_){ }
    return undefined;
  }
}

function _runOptionalPostStartUiStep(label, fn){
  try{
    return typeof fn==='function'?fn():undefined;
  }catch(e){
    const message=e&&e.message?e.message:String(e||'unknown error');
    try{console.warn('[webui] optional post-start UI step failed', label, message);}catch(_){ }
    return undefined;
  }
}

function _sessionTitleLooksDefaultOrProvisional(titleText, provisionalText){
  const title=String(titleText||'').replace(/\s+/g,' ').trim();
  if(!title||title==='Untitled'||title==='New Chat')return true;
  const provisional=String(provisionalText||'').replace(/\s+/g,' ').trim().slice(0,64);
  return !!provisional&&title===provisional;
}

function _firstUserMessageTitleCandidate(){
  const first=(S.messages||[]).find(m=>m&&m.role==='user'&&m.content);
  return first?String(first.content||'').trim().slice(0,64):'';
}

function applySessionTitleUpdate(sid, titleText, options={}){
  const newTitle=String(titleText||'').trim();
  if(!sid||!newTitle)return false;
  const row=(typeof _allSessions!=='undefined'&&Array.isArray(_allSessions))
    ? _allSessions.find(s=>s&&s.session_id===sid)
    : null;
  const currentTitle=S.session&&S.session.session_id===sid
    ? S.session.title
    : row&&row.title;
  if(!options.force){
    const expected=String(options.expectedCurrent||'').trim();
    const remembered=_sessionTitleProvisionalBySid.get(sid)||'';
    const provisionalCandidates=[options.provisionalText,remembered,_firstUserMessageTitleCandidate()];
    const allowed=(expected&&String(currentTitle||'').trim()===expected)
      || String(currentTitle||'').trim()===newTitle
      || provisionalCandidates.some(p=>_sessionTitleLooksDefaultOrProvisional(currentTitle, p));
    if(!allowed)return false;
  }
  if(S.session&&S.session.session_id===sid){
    S.session.title=newTitle;
    if(typeof syncTopbar==='function') syncTopbar();
  }
  if(row) row.title=newTitle;
  if(options.rememberProvisional) _sessionTitleProvisionalBySid.set(sid,newTitle);
  if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
  else if(typeof renderSessionList==='function') renderSessionList();
  return true;
}

async function send(){
  // Static guards expect _busyInputMode to stay near send() while the actual
  // read remains in the S.busy branch below.
  // _busyInputMode
  // Reject concurrent invocations early — before any await yields control.
  // If a send is already in-flight (e.g. queue drain), re-queue the message
  // instead of silently dropping it.
  if (_sendInProgress) {
    const _text=_composerTextWithPendingSelections().trim();
    // Use the in-flight session's sid, not the currently viewed session,
    // so the queued message goes to the chat that owns the active stream.
    const _targetSid=_sendInProgressSid||(S.session&&S.session.session_id);
    if(_text && _targetSid){
      const _modelState=_chatPayloadModelState();
      queueSessionMessage(_targetSid,{text:_text,files:[...S.pendingFiles],model:_modelState.model,model_provider:_modelState.model_provider,profile:S.activeProfile||'default'});
      _clearComposerAfterQueuedSelectionSend();
      S.pendingFiles=[];renderTray();
      updateQueueBadge(_targetSid);
      showToast(`Queued: "${_text.slice(0,40)}${_text.length>40?'…':''}"`,2000);
    }
    return;
  }
  _sendInProgress = true;
  try{
  let text=$('msg').value.trim();
  if(!text&&!S.pendingFiles.length&&!_pendingSelections.length){_sendInProgress=false;_sendInProgressSid=null;return;}
  // Don't send while an inline message edit is active
  if(document.querySelector('.msg-edit-area')){_sendInProgress=false;_sendInProgressSid=null;return;}
  _flushSelectionBlocksToComposer();
  text=$('msg').value.trim();
  if(!text&&!S.pendingFiles.length){_sendInProgress=false;_sendInProgressSid=null;return;}

  // Dismiss handoff hint when user sends a message (resets seen_at).
  if(S.session&&S.session.session_id&&typeof _dismissHandoffHint==='function'){
    _dismissHandoffHint(S.session.session_id);
  }

  const compressionRunning=typeof isCompressionUiRunning==='function'&&isCompressionUiRunning();
  _clearStaleBusyStateBeforeSend({compressionRunning});
  // If busy or a manual compression is still running, handle based on busy_input_mode
  if(S.busy||compressionRunning){
    if(text){
      if(!S.session){await newSession();await renderSessionList();}
      // Busy-control slash commands must be intercepted HERE, before the
      // busyMode routing block, so the user can always type /steer, /interrupt,
      // /queue, /terminal, /goal, or /yolo while the agent is running and have
      // them execute immediately.
      // Without this intercept they fall through to the queue and execute after
      // the current turn ends — by which point there is no active stream and
      // cmdSteer / cmdInterrupt say "No active task to stop."
      if(text.startsWith('/')){
        const _pc=typeof parseCommand==='function'&&parseCommand(text);
        if(_pc&&['steer','interrupt','queue','terminal','goal','yolo'].includes(_pc.name)){
          const _bc=COMMANDS.find(c=>c.name===_pc.name);
          if(_bc){
            $('msg').value='';autoResize();
            await _bc.fn(_pc.args);
            return;
          }
        }
      }
      const busyMode=window._busyInputMode||'queue';
      if(busyMode==='steer'&&S.activeStreamId&&typeof _trySteer==='function'){
        // Real steer: clear the input first so the user gets immediate
        // feedback, then ship the steer payload via /api/chat/steer.
        // _trySteer falls back to queue+cancel internally if the agent
        // isn't running / cached / steer-capable.
        $('msg').value='';autoResize();
        // Do NOT clear pendingFiles yet — _trySteer may fall back to
        // interrupt+queue and needs the files for queueSessionMessage.
        // _trySteer clears pendingFiles itself in the fallback path, and
        // the server returns accepted:true (no files sent) on success.
        await _trySteer(text, /*explicitSteer=*/false);
        // After _trySteer: clear any remaining files (success path).
        S.pendingFiles=[];renderTray();
      } else if(busyMode==='interrupt'){
        // Queue the message, then cancel so drain re-sends it.
        const _modelState=_chatPayloadModelState();
        queueSessionMessage(S.session.session_id,{text,files:[...S.pendingFiles],model:_modelState.model,model_provider:_modelState.model_provider,profile:S.activeProfile||'default'});
        updateQueueBadge(S.session.session_id);
        $('msg').value='';autoResize();
        S.pendingFiles=[];renderTray();
        if(S.activeStreamId&&typeof cancelStream==='function'){
          showToast(t('busy_interrupt_confirm'),2000);
          await cancelStream();
        } else {
          showToast(`Queued: "${text.slice(0,40)}${text.length>40?'…':''}"`,2000);
        }
      } else {
        // Default: queue mode (current behavior). Also the fallback for
        // 'steer' mode when no stream is active or _trySteer is unavailable.
        const _modelState=_chatPayloadModelState();
        queueSessionMessage(S.session.session_id,{text,files:[...S.pendingFiles],model:_modelState.model,model_provider:_modelState.model_provider,profile:S.activeProfile||'default'});
        $('msg').value='';autoResize();
        S.pendingFiles=[];renderTray();
        updateQueueBadge(S.session.session_id);
        showToast(`Queued: "${text.slice(0,40)}${text.length>40?'…':''}"`,2000);
      }
    }
    return;
  }
  if(S.session&&(S.session.read_only||S.session.is_read_only)){
    if(typeof showToast==='function') showToast('Read-only imported sessions cannot be modified.',3000);
    return;
  }
  let _slashDisplayTextOverride=null;
  // Slash command intercept -- local commands handled without agent round-trip.
  // We push the user message BEFORE running the handler for echo-worthy
  // commands so chat order is correct: some handlers (e.g. cmdHelp) push
  // their assistant response synchronously.  If we pushed AFTER, S.messages
  // would be [assistant, user] and the chat would show the response above
  // the user's own input — reverse chronological order (#840 ordering bug).
  if(text.startsWith('/')&&!S.pendingFiles.length){
    const _parsedCmd=parseCommand(text);
    const _cmd=_parsedCmd?COMMANDS.find(c=>c.name===_parsedCmd.name):null;
    if(_cmd){
      let _pushedUser=false;
      if(!_cmd.noEcho){
        if(!S.session){await newSession();await renderSessionList();}
        S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
        _pushedUser=true;
        renderMessages();
      }
      // Run the handler directly (we already looked it up).  If it returns
      // false it's opting out — e.g. /reasoning <level> falls through so the
      // agent sees the raw text.  Roll back the echo push in that case so
      // the normal send path doesn't duplicate it.
      if(_cmd.fn(_parsedCmd.args)===false){
        if(_pushedUser){S.messages.pop();renderMessages();}
        // Fall through to normal send path
      } else {
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
    }
    if(_parsedCmd&&!_cmd){
      const _agentCmd=typeof getAgentCommandMetadata==='function'
        ? await getAgentCommandMetadata(_parsedCmd.name)
        : null;
      if(_agentCmd&&_agentCmd.cli_only){
        if(!S.session){await newSession();await renderSessionList();}
        S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
        S.messages.push({role:'assistant',content:cliOnlyCommandResponse(_parsedCmd.name,_agentCmd),_ts:Date.now()/1000});
        renderMessages();
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
      const _agentCmdName=String(_agentCmd&&_agentCmd.name||_parsedCmd&&_parsedCmd.name||'').trim().toLowerCase();
      if(_AGENT_COMMANDS_RUN_ON_WEBUI.has(_agentCmdName)){
        if(!S.session){await newSession();await renderSessionList();}
        S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
        let _agentOutput='(no output)';
        try{
          _agentOutput=typeof executeAgentCommand==='function'
            ? await executeAgentCommand(text,_agentCmd||{name:_agentCmdName})
            : 'Agent command runtime unavailable in WebUI.';
        }catch(e){
          _agentOutput=`Agent command error: ${e&&e.message||e}`;
        }
        S.messages.push({role:'assistant',content:String(_agentOutput||'(no output)'),_ts:Date.now()/1000});
        renderMessages();
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
      if(_agentCmd&&_agentCmd.category==='Plugin'){
        if(!S.session){await newSession();await renderSessionList();}
        S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
        let _pluginOutput='(no output)';
        try{
          _pluginOutput=typeof executeAgentPluginCommand==='function'
            ? await executeAgentPluginCommand(text,_agentCmd)
            : 'Plugin command runtime unavailable in WebUI.';
        }catch(e){
          _pluginOutput=`Plugin command error: ${e&&e.message||e}`;
        }
        S.messages.push({role:'assistant',content:String(_pluginOutput||'(no output)'),_ts:Date.now()/1000});
        renderMessages();
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
      const _bundleCmd=!_agentCmd&&typeof getBundleCommandMetadata==='function'
        ? await getBundleCommandMetadata(_parsedCmd.name)
        : null;
      if(_bundleCmd){
        try{
          const _bundleResolved=typeof resolveBundleCommand==='function'
            ? await resolveBundleCommand(text,_bundleCmd)
            : null;
          const _bundleMessage=String(_bundleResolved&&_bundleResolved.message||'').trim();
          if(!_bundleMessage) throw new Error('Bundle command runtime returned no invocation text.');
          _slashDisplayTextOverride=text;
          text=_bundleMessage;
        }catch(e){
          if(!S.session){await newSession();await renderSessionList();}
          S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
          S.messages.push({role:'assistant',content:`Bundle command error: ${e&&e.message||e}`,_ts:Date.now()/1000});
          renderMessages();
          $('msg').value='';autoResize();hideCmdDropdown();return;
        }
      }
    }
  }
  if(!S.session){await newSession();await renderSessionList();}

  const activeSid=S.session.session_id;
  _sendInProgressSid=activeSid;

  setComposerStatus(S.pendingFiles&&S.pendingFiles.length?'Uploading…':'');
  let uploaded=[];
  try{uploaded=await uploadPendingFiles();}
  catch(e){if(!text){setComposerStatus(`Upload error: ${e.message}`);return;}}
  // Clear the uploading status now that upload is done — if we don't clear here
  // it stays visible for the entire duration of the agent stream, since
  // setComposerStatus('') is only called in setBusy(false), not setBusy(true).
  setComposerStatus('');

  const uploadedNames=uploaded.map(u=>u.name||u);
  const uploadedPaths=uploaded.map(u=>u&&u.path?u.path:(u&&u.name?u.name:(u&&u.filename?u.filename:u)));
  let msgText=text;
  if(uploaded.length&&!msgText)msgText=`I've uploaded ${uploaded.length} file(s): ${uploadedPaths.join(', ')}`;
  else if(uploaded.length)msgText=`${text}\n\n[Attached files: ${uploadedPaths.join(', ')}]`;
  if(_forcedSkillDirectivePending){
    const _pending=_forcedSkillDirectivePending;
    if(!_pending.sessionId||_pending.sessionId===activeSid){
      const _directivePayload = await _pending.promise;
      if(_forcedSkillDirectivePending===_pending)_forcedSkillDirectivePending = null;
      if(_directivePayload){
        const _directive = typeof _directivePayload==='string'
          ? _directivePayload
          : String(_directivePayload.directive||'').trim();
        const _forcedSkillName = typeof _directivePayload==='string'
          ? ''
          : String(_directivePayload.name||'').trim();
        const _forcedSkillContent = typeof _directivePayload==='string'
          ? ''
          : String(_directivePayload.content||'').trim();
        const _forcedSkillBlock = _forcedSkillName&&_forcedSkillContent
          ? `[FORCED SKILL CONTEXT: ${_forcedSkillName}]\n${_forcedSkillContent}\n[/FORCED SKILL CONTEXT]`
          : '';
        msgText=`${_directive}${_forcedSkillBlock?`\n\n${_forcedSkillBlock}`:''}\n\n${msgText||''}`.trim();
      }
    }
  }
  if(!msgText){setComposerStatus('Nothing to send');return;}

  $('msg').value='';autoResize();
  // Clear persisted composer draft since message was sent.
  if (activeSid && typeof _clearComposerDraft === 'function') _clearComposerDraft(activeSid);
  const displayText=_slashDisplayTextOverride||text||(uploaded.length?`Uploaded: ${uploadedNames.join(', ')}`:'(file upload)');
  const userMsg={role:'user',content:displayText,attachments:uploaded.length?uploadedNames:undefined,_ts:Date.now()/1000};
  S.toolCalls=[];  // clear tool calls from previous turn
  clearLiveToolCards();  // clear any leftover live cards from last turn
  let optimisticMessages;
  try{
    S.messages.push(userMsg);renderMessages();setBusy(true);
    if(S.session&&!S.session.pending_started_at) S.session.pending_started_at=Date.now()/1000;
    if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
    else appendThinking('',{pending:true});
    // First optimistic pass: make the local user turn visible before /api/chat/start
    // can save pending state on the server.
    _runOptionalPreStartUiStep('upsertActiveSessionForLocalTurn.initial', ()=>{
      if(typeof upsertActiveSessionForLocalTurn==='function'){
        upsertActiveSessionForLocalTurn({title:displayText.slice(0,64),messageCount:S.messages.length,timestampMs:Date.now()});
      }
    });
    optimisticMessages=[...S.messages];
    INFLIGHT[activeSid]={messages:optimisticMessages,uploaded:uploadedNames,toolCalls:[]};
    if(typeof saveInflightState==='function'){
      saveInflightState(activeSid,{streamId:null,messages:INFLIGHT[activeSid].messages,uploaded:uploadedNames,toolCalls:[]});
    }
    _runOptionalPreStartUiStep('renderSessionListFromCache.initial', ()=>{
      if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
    });
    _runOptionalPreStartUiStep('startApprovalPolling.prestart', ()=>startApprovalPolling(activeSid));
    _runOptionalPreStartUiStep('startClarifyPolling.prestart', ()=>startClarifyPolling(activeSid));
    _runOptionalPreStartUiStep('fetchYoloState.prestart', ()=>_fetchYoloState(activeSid));  // sync YOLO pill with backend state
    S.activeStreamId = null;  // will be set after stream starts
    _runOptionalPreStartUiStep('updateSendBtn.prestart', ()=>{
      if(typeof updateSendBtn==='function') updateSendBtn();
    });

    // Set provisional title from user message immediately so session appears
    // in the sidebar right away with a meaningful name. /api/chat/start persists
    // the server-side provisional title and may refine this optimistic text.
    if(S.session&&(S.session.title==='Untitled'||!S.session.title)){
      const provisionalTitle=displayText.slice(0,64);
      _runOptionalPreStartUiStep('applySessionTitleUpdate.provisional', ()=>{
        applySessionTitleUpdate(activeSid, provisionalTitle, {force:true, rememberProvisional:true});
      });
      _runOptionalPreStartUiStep('upsertActiveSessionForLocalTurn.provisional', ()=>{
        if(typeof upsertActiveSessionForLocalTurn==='function'){
          // Second optimistic pass: carry the provisional title into the cached row
          // without re-fetching /api/sessions before pending state exists server-side.
          upsertActiveSessionForLocalTurn({title:provisionalTitle,messageCount:S.messages.length,timestampMs:Date.now()});
        }
      });
    } else if(typeof upsertActiveSessionForLocalTurn==='function'){
      _runOptionalPreStartUiStep('upsertActiveSessionForLocalTurn.titled', ()=>{
        upsertActiveSessionForLocalTurn({title:S.session&&S.session.title||displayText.slice(0,64),messageCount:S.messages.length,timestampMs:Date.now()});
      });
    } else {
      _runOptionalPreStartUiStep('renderSessionListFromCache.prestart', ()=>{
        renderSessionListFromCache();  // ensure it's visible even if already titled
      });
    }
  }catch(preStartError){
    // The user turn must reach /api/chat/start even if local optimistic UI
    // bookkeeping (render cache, storage quota, sidebar reconciliation, etc.)
    // throws. Otherwise the pane can show a user bubble + spinner while the
    // backend never receives the turn.
    const message=preStartError&&preStartError.message?preStartError.message:String(preStartError||'unknown error');
    try{console.warn('[webui] pre-start optimistic UI failed; continuing to /api/chat/start', message);}catch(_){ }
    if(!S.messages.includes(userMsg)) S.messages.push(userMsg);
    optimisticMessages=[...S.messages];
    INFLIGHT[activeSid]={messages:optimisticMessages,uploaded:uploadedNames,toolCalls:[]};
    try{setBusy(true);}catch(_){S.busy=true;}
    if(S.session&&!S.session.pending_started_at) S.session.pending_started_at=Date.now()/1000;
    S.activeStreamId=null;
    if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
  }

  // Start the agent via POST, get a stream_id back
  let streamId;
  let postStartData;
  let modelStateForPostStart;
  let explicitPickForPostStart;
  try{
    const _modelState=_chatPayloadModelState();
    modelStateForPostStart=_modelState;
    const _pendingPick=(typeof _readPendingSessionModel==='function')
      ? _readPendingSessionModel(activeSid)
      : null;
    const _pendingPickMatch=_pendingPick
      && _pendingPick.model===_modelState.model
      && String(_pendingPick.model_provider||'')===String(_modelState.model_provider||'');
    // ── Persisted cross-provider pick (#3737 follow-up) ──
    // The onchange marker is consumed after the first send, so subsequent sends
    // lose explicit_model_pick and the server "repairs" the model back to the
    // profile default.  When the session has a non-default model from a different
    // provider than the profile's active provider, treat every send as explicit
    // so the server honors the user's choice across the entire conversation.
    const _defaultModel=(typeof window!=='undefined' && window._defaultModel)||'';
    const _activeProvider=(typeof window!=='undefined' && window._activeProvider)||null;
    const _isCrossProviderPick = _modelState.model
      && _modelState.model_provider
      && _defaultModel
      && _activeProvider
      && _modelState.model !== _defaultModel
      && String(_modelState.model_provider||'') !== String(_activeProvider||'');
    const _explicitPick = _pendingPickMatch || _isCrossProviderPick;
    // Consume the pending explicit-pick marker for THIS send only. The marker is
    // recorded on modelSelect.onchange and intentionally kept (not cleared on
    // session-update) so it survives the normal pick→update→send flow; clear it here
    // once read so a later send of an unchanged dropdown isn't treated as an explicit
    // pick. (#3739/#3737, Codex catch)
    if(_pendingPickMatch && typeof _clearPendingSessionModel==='function') _clearPendingSessionModel(activeSid);
    explicitPickForPostStart=_explicitPick;
    const startData=await api('/api/chat/start',{method:'POST',body:JSON.stringify({
      session_id:activeSid,message:msgText,
      // S.session.model remains authoritative; the helper only resolves a
      // matching provider fallback for the same outgoing model.
      model:_modelState.model,workspace:S.session.workspace,
      model_provider:_modelState.model_provider,
      profile:S.activeProfile||S.session.profile||'default',
      explicit_model_pick:_explicitPick||undefined,
      attachments:uploaded.length?uploaded:undefined
    })});
    postStartData = startData;
  }catch(e){
    const errMsg=String((e&&e.message)||'');
    // If /api/chat/start returns 404, the session was deleted server-side
    // (its sidecar is gone) while GET kept returning a CLI stub (#2782). Strip
    // the stale /session/<id> URL and clear localStorage so a reload does not
    // re-inject the dead id via _sessionIdFromLocation(), then reset to the
    // empty state instead of pushing a confusing error bubble into the chat.
    if(e&&e.status===404){
      try{ localStorage.removeItem('hermes-webui-session'); }catch(_){ }
      try{
        if(typeof _appRootPath==='function') history.replaceState(null,'',_appRootPath());
        else history.replaceState(null,'',window.location.pathname.replace(/\/session\/[^/]+/,'')+window.location.search);
      }catch(_){ }
      delete INFLIGHT[activeSid];
      if(typeof clearInflightState==='function') clearInflightState(activeSid);
      stopApprovalPolling();
      stopClarifyPolling();
      if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard(true);
      if(!_clarifySessionId || _clarifySessionId===activeSid) hideClarifyCard(true, 'terminal');
      removeThinking();
      S.session=null;S.messages=[];
      setBusy(false);setComposerStatus('');
      if(typeof clearOptimisticSessionStreaming==='function') clearOptimisticSessionStreaming(activeSid);
      if(typeof renderMessages==='function') renderMessages();
      if($('emptyState')) $('emptyState').style.display='';
      if($('msgInner')) $('msgInner').innerHTML='';
      if(typeof renderSessionList==='function') void renderSessionList();
      return;
    }
    const conflictActiveStream=/session already has an active stream/i.test(errMsg);
    if(conflictActiveStream){
      delete INFLIGHT[activeSid];
      if(typeof clearInflightState==='function') clearInflightState(activeSid);
      stopApprovalPolling();
      stopClarifyPolling();
      // Keep the user's attempted turn by queueing it for after the current run.
      const _retryModelState=_chatPayloadModelState();
      queueSessionMessage(activeSid,{text:msgText,files:[],model:_retryModelState.model,model_provider:_retryModelState.model_provider,profile:S.activeProfile||'default'});
      updateQueueBadge(activeSid);
      showToast('Current session is still running. Reconnected and queued your message.',2600);
      try{
        await loadSession(activeSid);
        setComposerStatus('');
        return;
      }catch(_){
        // Fall through to standard error handling if session reload fails.
      }
    }

    delete INFLIGHT[activeSid];
    stopApprovalPolling();
    stopClarifyPolling();
    // Only hide approval card if it belongs to the session that just finished
    if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard(true);removeThinking();
    if(!_clarifySessionId || _clarifySessionId===activeSid) hideClarifyCard(true, 'terminal');
    S.messages.push({role:'assistant',content:`**Error:** ${errMsg}`});
    _queueDrainSid=activeSid;renderMessages();setBusy(false);setComposerStatus(`Error: ${errMsg}`);
    if(typeof clearOptimisticSessionStreaming==='function') clearOptimisticSessionStreaming(activeSid);
    // Reconcile with server truth after immediately clearing the optimistic spinner.
    if(typeof renderSessionList==='function') void renderSessionList();
    return;
  }

  const startData = postStartData || {};
  streamId = postStartData ? postStartData.stream_id : null;
  S.activeStreamId = streamId;
  // setBusy(true) already ran with activeStreamId=null; refresh now that we
  // have a stream id so the primary button can switch to Stop (see
  // getComposerPrimaryAction).
  if(typeof updateSendBtn==='function') updateSendBtn();
  _runOptionalPostStartUiStep('post-start ui/bookkeeping', ()=>{
    const _modelState=modelStateForPostStart || _chatPayloadModelState();
    const _explicitPick=explicitPickForPostStart;
    if(startData&&startData.title) applySessionTitleUpdate(activeSid, startData.title, {provisionalText:displayText.slice(0,64), rememberProvisional:true});

    if(startData&&startData.effective_model && S.session){
      const _sentModel=_modelState&&_modelState.model;
      if(_explicitPick && _sentModel && startData.effective_model!==_sentModel && typeof showToast==='function'){
        showToast('Model '+_sentModel+' changed to '+startData.effective_model+' — profile provider mismatch', 5000);
      }
      S.session.model=startData.effective_model;
      S.session.model_provider=startData.effective_model_provider||S.session.model_provider||null;
      localStorage.setItem('hermes-webui-model', startData.effective_model);
      if(typeof _writePersistedModelState==='function') _writePersistedModelState(startData.effective_model,S.session.model_provider||null);
      if($('modelSelect')) _applyModelToDropdown(startData.effective_model, $('modelSelect'),S.session.model_provider||null);
      if(typeof syncTopbar==='function') syncTopbar();
    }else if(startData&&startData.effective_model_provider && S.session){
      S.session.model_provider=startData.effective_model_provider;
      if(typeof _writePersistedModelState==='function') _writePersistedModelState(S.session.model||'',S.session.model_provider||null);
      if($('modelSelect')&&typeof _applyModelToDropdown==='function') _applyModelToDropdown(S.session.model||'', $('modelSelect'), S.session.model_provider||null);
      if(typeof syncModelChip==='function') syncModelChip();
      if(typeof syncTopbar==='function') syncTopbar();
    }

    if(S.session&&typeof startData.pending_started_at==='number'){
      S.session.pending_started_at=startData.pending_started_at;
    }
    if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
    else if(typeof appendThinking==='function') appendThinking('',{pending:true});
    // setBusy(true) already ran with activeStreamId=null; refresh now that we
    // have a stream id so the primary button can switch to Stop (see
    // getComposerPrimaryAction).
    if(typeof updateSendBtn==='function') updateSendBtn();
    if(S.session&&S.session.session_id===activeSid){
      S.session.active_stream_id = streamId;
    }
    if(S.session&&S.session.session_id===activeSid&&typeof showLiveRunStatus==='function'){
      const _startedAt=typeof startData?.pending_started_at==='number'
        ? startData.pending_started_at
        : (S.session.pending_started_at||Date.now()/1000);
      showLiveRunStatus(activeSid,{startedAt:_startedAt});
    }
    if(typeof upsertActiveSessionForLocalTurn==='function'){
      // Third optimistic pass: stream_id is now known, so the row can reconcile
      // against real active-stream metadata before the background refresh lands.
      upsertActiveSessionForLocalTurn({title:S.session&&S.session.title||displayText.slice(0,64),messageCount:S.messages.length,timestampMs:Date.now()});
    }
    if(!INFLIGHT[activeSid]){
      INFLIGHT[activeSid]={messages:optimisticMessages,uploaded:uploadedNames,toolCalls:[]};
    }
    const currentInflight=INFLIGHT[activeSid];
    markInflight(activeSid, streamId);
    if(typeof saveInflightState==='function'){
      saveInflightState(activeSid,{streamId,messages:currentInflight.messages||optimisticMessages,uploaded:uploadedNames,toolCalls:currentInflight.toolCalls||[]});
    }
    // Refresh session list so background streaming indicators appear immediately for the
    // session that was just started and any others that may already be running.
    if(typeof renderSessionList === 'function') {
      void renderSessionList();
    }
  });

  // Open SSE stream and render tokens live
  attachLiveStream(activeSid, streamId, uploadedNames);

  }finally{ _sendInProgress=false; _sendInProgressSid=null; }
}

const LIVE_STREAMS={};

// #4416: track whether the tab was hidden at ANY point during a live stream, so
// the response-complete notification fires for a backgrounded tab even when
// Chromium throttles the background-tab SSE and delivers the `done` event LATE
// (after the user returns, when document.hidden already reads false). Each entry
// is STREAM-OWNED ({streamId, wasHidden}) so a stale entry left by a non-`done`
// terminal path (apperror/cancel/stream-error/reconnect-no-active) can never be
// mis-attributed to a later stream for the same session id — a reconnect only
// keeps the prior state when the streamId matches. One idempotent
// visibilitychange listener (never leaks) flips wasHidden on all active entries.
const _STREAM_WAS_HIDDEN={};
let _streamHiddenTrackerBound=false;
function _bindStreamHiddenTracker(){
  if(_streamHiddenTrackerBound||typeof document==='undefined'||typeof document.addEventListener!=='function') return;
  _streamHiddenTrackerBound=true;
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden){ for(const k in _STREAM_WAS_HIDDEN){ const e=_STREAM_WAS_HIDDEN[k]; if(e) e.wasHidden=true; } }
  });
}
function _clearStreamHidden(sid, streamId){
  // Clear only when we own the current stream's entry (or unconditionally when
  // streamId is omitted). Prevents a terminal path for an old stream from wiping
  // a newer stream's tracker.
  if(!sid) return;
  const e=_STREAM_WAS_HIDDEN[sid];
  if(!e) return;
  if(streamId&&e.streamId&&e.streamId!==streamId) return;
  delete _STREAM_WAS_HIDDEN[sid];
}

function closeLiveStream(sessionId, streamId, source){
  const live=LIVE_STREAMS[sessionId];
  if(!live) return;
  if(streamId&&live.streamId!==streamId) return;
  if(source&&live.source!==source) return;
  // Snapshot the current live-turn DOM BEFORE tearing the stream down. The
  // per-event snapshot (snapshotLiveTurn) only fires on content/tool_complete
  // SSE events, so switching away during a quiet window (mid tool-exec, silent
  // thinking) would leave a stale-or-absent snapshot — on switch-back
  // restoreLiveTurnHtmlForSession() then fails and loadSession()'s fallback
  // rebuilds with an EMPTY appendThinking(), permanently losing the streamed
  // thinking/tool content (only the elapsed clock survives). Capturing here
  // guarantees switch-back restores the exact state shown at switch-away. (#3668)
  if(typeof snapshotLiveTurnHtmlForSession==='function') snapshotLiveTurnHtmlForSession(sessionId);
  // Stop the live footer timer/status for the pane that is being detached; the
  // reattach path will rebuild it from INFLIGHT/server state if the user returns.
  if(typeof _clearLiveRunStatusTimer==='function') _clearLiveRunStatusTimer(sessionId);
  if(typeof hideLiveRunStatus==='function') hideLiveRunStatus(sessionId);
  try{if(live.source&&live.source.readyState!==2)live.source.close();}catch(_){ }
  delete LIVE_STREAMS[sessionId];
  // closeLiveStream() is called during session-switch teardown for any session
  // the user is no longer viewing. The stream is still active on the server,
  // so mark the in-memory INFLIGHT entry for reattach — otherwise
  // loadSession() returning to this session skips the reattach branch
  // (`INFLIGHT.reattach` was only set by the storage-load path) and the SSE
  // is never reopened. The user then sees no streamed tokens until the LLM
  // finishes and a metadata refresh swaps in the final reply.
  // If the stream is terminating cleanly, _clearOwnerInflightState() has
  // already deleted INFLIGHT[sessionId], so this is a safe no-op.
  if(INFLIGHT[sessionId]){
    INFLIGHT[sessionId].reattach=true;
    // The browser-side INFLIGHT snapshot is only a compact tail cache. After a
    // session switch it cannot be treated as the full live turn; rebuild from
    // the durable run journal instead so earlier prose/tool rows are not lost.
    INFLIGHT[sessionId].journalReplayFromStart=true;
    if(typeof saveInflightState==='function'){
      saveInflightState(sessionId,{
        streamId:live.streamId||streamId||null,
        messages:INFLIGHT[sessionId].messages||[],
        uploaded:INFLIGHT[sessionId].uploaded||[],
        toolCalls:INFLIGHT[sessionId].toolCalls||[],
        lastAssistantText:INFLIGHT[sessionId].lastAssistantText||'',
        lastReasoningText:INFLIGHT[sessionId].lastReasoningText||'',
        lastRunJournalSeq:INFLIGHT[sessionId].lastRunJournalSeq||0,
        journalReplayFromStart:true,
        currentActivityBurstId:INFLIGHT[sessionId].currentActivityBurstId||0,
        currentLiveSegmentSeq:INFLIGHT[sessionId].currentLiveSegmentSeq||0,
        activityBurstAnchors:Array.isArray(INFLIGHT[sessionId].activityBurstAnchors)?INFLIGHT[sessionId].activityBurstAnchors:[],
      });
    }
  }
}

function closeOtherLiveStreams(activeSid){
  // Keep the live token SSE connection scoped to the conversation pane the user
  // is actually viewing. Background sessions still show running/finished state
  // through the session list and can reattach when selected, but they should not
  // keep one EventSource each and exhaust the browser connection pool (#2313).
  for(const sid of Object.keys(LIVE_STREAMS)){
    if(sid!==activeSid) closeLiveStream(sid);
  }
}

function attachLiveStream(activeSid, streamId, uploaded=[], options={}){
  if(!activeSid||!streamId) return;
  const reconnecting=!!options.reconnecting;
  // #4416: start (or, on reconnect for the SAME stream, keep) tracking whether
  // the tab was hidden during this stream so the done-notification fires for a
  // backgrounded tab. A reconnect with a different streamId re-seeds (the old
  // entry belonged to a prior stream).
  _bindStreamHiddenTracker();
  {
    const _prev=_STREAM_WAS_HIDDEN[activeSid];
    const _keep=reconnecting&&_prev&&_prev.streamId===streamId;
    if(!_keep){
      _STREAM_WAS_HIDDEN[activeSid]={streamId,wasHidden:(typeof document!=='undefined'&&!!document.hidden)};
    }
  }
  if(!INFLIGHT[activeSid]) INFLIGHT[activeSid]={messages:[...S.messages],uploaded:[...uploaded],toolCalls:[]};
  else {
    if(uploaded.length) INFLIGHT[activeSid].uploaded=[...uploaded];
    if(!Array.isArray(INFLIGHT[activeSid].toolCalls)) INFLIGHT[activeSid].toolCalls=[];
  }
  if(!Array.isArray(INFLIGHT[activeSid].activityBurstAnchors)) INFLIGHT[activeSid].activityBurstAnchors=[];
  if(INFLIGHT[activeSid].currentActivityBurstId===undefined) INFLIGHT[activeSid].currentActivityBurstId=0;
  if(INFLIGHT[activeSid].currentLiveSegmentSeq===undefined) INFLIGHT[activeSid].currentLiveSegmentSeq=0;
  let assistantText='';
  let reasoningText='';
  if(S.session&&S.session.session_id===activeSid&&S.activeStreamId===streamId&&typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
  const existingLive=LIVE_STREAMS[activeSid];
  if(
    existingLive&&existingLive.streamId===streamId&&existingLive.source&&
    // During explicit reconnects, only reuse a proven-open transport. A stale
    // CONNECTING EventSource can survive in page state while the server has no
    // subscriber, which leaves the live pane blank forever.
    (typeof EventSource==='undefined'||
      existingLive.source.readyState===EventSource.OPEN||
      (!reconnecting&&existingLive.source.readyState===EventSource.CONNECTING))
  ){
    // Phase D: restore bottom run status on reattach after the Worklog shell
    // exists. There is no stale transport teardown in this branch.
    if(reconnecting && S.activeStreamId && typeof showLiveRunStatus==='function'){
      const _startedAt=(S.session&&S.session.pending_started_at)||Date.now()/1000;
      showLiveRunStatus(activeSid,{startedAt:_startedAt});
    }
    return;
  }
  closeOtherLiveStreams(activeSid);
  closeLiveStream(activeSid);
  if(!reconnecting&&typeof resetTurnWorkspaceMutations==='function') resetTurnWorkspaceMutations();
  if(!reconnecting&&typeof _resetStreamScrollFollow==='function') _resetStreamScrollFollow();
  // Phase D: restore bottom run status after closeLiveStream(); that helper
  // hides the status while tearing down stale EventSource ownership.
  if(reconnecting && S.activeStreamId && typeof showLiveRunStatus==='function'){
    const _startedAt=(S.session&&S.session.pending_started_at)||Date.now()/1000;
    showLiveRunStatus(activeSid,{startedAt:_startedAt});
  }

  // On reconnect, restore accumulated text from INFLIGHT so we don't lose
  // progress made before the session switch. Without this the closure starts
  // empty and tokens arriving on the new SSE connection append to nothing —
  // the already-rendered content vanishes.
  const _liveInflightAssistantMessages = reconnecting
    ? ((INFLIGHT[activeSid]&&Array.isArray(INFLIGHT[activeSid].messages))
      ? INFLIGHT[activeSid].messages.filter(m=>m&&m.role==='assistant'&&m._live)
      : [])
    : [];
  const _liveInflightAssistant = _liveInflightAssistantMessages.length===1
    ? _liveInflightAssistantMessages[0]
    : null;
  const _fullInflightAssistant = (INFLIGHT[activeSid]&&INFLIGHT[activeSid].lastAssistantText) || '';
  const _joinedInflightSegments = _liveInflightAssistantMessages.length>1
    ? _liveInflightAssistantMessages.map(m=>m&&m.content?String(m.content).trim():'').filter(Boolean).join('\n\n')
    : '';
  const _lastLiveAssistant = reconnecting
    ? (_liveInflightAssistantMessages.length>1
      ? (_fullInflightAssistant || _joinedInflightSegments)
      : (_liveInflightAssistant
        ? (_fullInflightAssistant || _liveInflightAssistant.content || '')
        : _fullInflightAssistant))
    : '';
  const _lastLiveReasoning = reconnecting
    ? (_liveInflightAssistant&&_liveInflightAssistant.reasoning)
      || (INFLIGHT[activeSid]&&INFLIGHT[activeSid].lastReasoningText)
      || ''
    : '';
  assistantText = _lastLiveAssistant ? _lastLiveAssistant : '';
  reasoningText=_lastLiveReasoning ? _lastLiveReasoning : '';
  let liveReasoningText = reasoningText;
  let visibleInterimSnippets=[];
  let _latestGoalStatus=null;
  let _pendingGoalContinuation=null;
  let assistantRow=null;
  let assistantBody=null;
  // On reconnect with recorded burst anchors, the rendered DOM has multiple
  // live assistant segments — one per anchor plus a tail. New tokens belong to
  // the TAIL segment only.
  let segmentStart=(()=>{
    if(!reconnecting) return 0;
    const inflight=INFLIGHT[activeSid];
    if(!inflight) return 0;
    const anchors=Array.isArray(inflight.activityBurstAnchors)?inflight.activityBurstAnchors:[];
    const textLen=String(assistantText||'').length;
    let lastEnd=0;
    for(const a of anchors){
      const end=Number(a&&a.textEnd);
      if(Number.isFinite(end)&&end>lastEnd&&end<=textLen) lastEnd=end;
    }
    return lastEnd;
  })();
  // If reconnect resumes exactly at the last recorded boundary, there is no
  // projected tail segment yet. The next token must create a fresh segment
  // after the last Activity group instead of rewriting the previous burst's
  // text segment.
  let _freshSegment=reconnecting&&segmentStart>0&&segmentStart>=String(assistantText||'').length;
  // streaming-markdown state: incremental DOM-building parser per segment
  let _smdParser=null;     // current smd parser instance (null until first content)
  let _smdWrittenLen=0;    // how many chars of displayText have been fed to smd parser
  let _smdWrittenText='';  // exact displayText snapshot used for prefix-alignment checks
  let _streamingKatexTimer=null; // throttles live KaTeX scans while smd writes deltas
  // On reconnect, the assistantBody already has partial smd-rendered content.
  // We clear it on first new token and restart the parser from the reconnect point.
  let _smdReconnect=reconnecting;
  function _isActiveSession(){
    return !!(S.session&&S.session.session_id===activeSid);
  }
  function _ownsActiveStreamOrBackground(){
    return !_isActiveSession() || S.activeStreamId===streamId;
  }
  function _bailOutOfTerminalEventsFromStaleStream(source){
    if(_ownsActiveStreamOrBackground()) return false;
    // This stale stream no longer owns the session — schedule cleanup of ITS own
    // anchor registry (identity-guarded, so it can't clobber the newer stream's
    // registry for the same session) before closing. (Codex leak catch.)
    _scheduleAnchorRegistryCleanup(120000);
    _closeSource(source);
    return true;
  }
  function _clearActivePaneInflightIfOwner(){
    if(_isActiveSession()) clearInflight();
  }
  function _approvalBelongsToOwner(){
    return _approvalSessionId===activeSid||(!_approvalSessionId&&_isActiveSession());
  }
  function _clarifyBelongsToOwner(){
    return _clarifySessionId===activeSid||(!_clarifySessionId&&_isActiveSession());
  }
  function _clearApprovalForOwner(){
    _clearApprovalPendingForSession(activeSid);
    if(!_approvalBelongsToOwner()) return;
    stopApprovalPolling();
    hideApprovalCard(true);
  }
  function _clearClarifyForOwner(reason){
    _clearClarifyPendingForSession(activeSid);
    if(!_clarifyBelongsToOwner()) return;
    stopClarifyPolling();
    hideClarifyCard(true, reason||'terminal');
  }
  function _clearOwnerInflightState(){
    if(_isActiveSession() && S.activeStreamId!==streamId) return;
    delete INFLIGHT[activeSid];
    clearInflightState(activeSid);
    _clearActivePaneInflightIfOwner();
  }
  function _isMarkerOnlyAssistantMessage(m){
    if(!m||m.role!=='assistant') return false;
    const text=String(typeof msgContent==='function'?msgContent(m):(m.content||''));
    return typeof _isPreservedCompressionTaskListMarkerOnlyText==='function'
      && _isPreservedCompressionTaskListMarkerOnlyText(text);
  }
  function _streamRecoveryControlMessageText(text){
    const normalized=String(text||'').replace(/\s+/g,' ').trim();
    if(!normalized) return false;
    const systemRecovery=/^\[System:/i.test(normalized)
      && /previous response was cut off by a network error/i.test(normalized)
      && /continue exactly where you left off/i.test(normalized);
    const backendRecovery=/^the live worker stopped before this run finished\.?$/i.test(normalized);
    return !!(systemRecovery || backendRecovery);
  }
  function _streamRecoveryControlMessage(m){
    if(!m||m.role==='tool') return false;
    if(m.recovery_control===true) return true;
    // Backward-compat ONLY for pre-marker persisted sessions: match the two
    // fully-anchored synthetic recovery strings. Do NOT fall back to
    // provider_details_label — a genuine "Response interrupted" card the user
    // SHOULD see also carries the 'Interruption details' label, and filtering
    // on it would drop a real interruption from the transcript (the inverse
    // data-loss class flagged on the sibling #3300). Marker + strict text only.
    const text=String(typeof msgContent==='function'?msgContent(m):(m.content||''));
    return _streamRecoveryControlMessageText(text);
  }
  function _filterRecoveryControlMessages(messages){
    if(!Array.isArray(messages)) return [];
    return messages.filter((m)=>!_streamRecoveryControlMessage(m));
  }
  function _replaceMarkerOnlyAssistantWithStreamError(messages){
    if(!Array.isArray(messages)) return false;
    const msg=[...messages].reverse().find(m=>m&&m.role==='assistant');
    if(!_isMarkerOnlyAssistantMessage(msg)) return false;
    msg.content='**Error:** No response received after context compression. Please retry.';
    msg.provider_details='The only assistant text returned for this turn was the internal preserved-task-list compression marker, so the WebUI replaced it with an explicit error instead of rendering the marker as a model response.';
    return true;
  }
  function _setActivePaneIdleIfOwner(){
    if(_isActiveSession()||!S.session||!INFLIGHT[S.session.session_id]){
      setBusy(false);
      setComposerStatus('');
      if(typeof setStatus==='function') setStatus('');
    }
  }
  function persistInflightState(){
    const inflight=INFLIGHT[activeSid];
    if(!inflight||typeof saveInflightState!=='function') return;
    saveInflightState(activeSid,{
      streamId,
      messages:inflight.messages||[],
      uploaded:inflight.uploaded||[...uploaded],
      toolCalls:inflight.toolCalls||[],
      lastAssistantText:inflight.lastAssistantText||'',
      lastReasoningText:inflight.lastReasoningText||'',
      lastRunJournalSeq:inflight.lastRunJournalSeq||0,
      journalReplayFromStart:!!inflight.journalReplayFromStart,
      anchorActivityScene:inflight.anchorActivityScene||null,
      currentActivityBurstId:inflight.currentActivityBurstId||0,
      currentLiveSegmentSeq:inflight.currentLiveSegmentSeq||0,
      activityBurstAnchors:Array.isArray(inflight.activityBurstAnchors)?inflight.activityBurstAnchors:[],
      todos:Array.isArray(inflight.todos)?inflight.todos:S.todos,
      todoStateMeta:inflight.todoStateMeta||S.todoStateMeta||null,
    });
  }
  function snapshotLiveTurn(){
    if(typeof snapshotLiveTurnHtmlForSession==='function') snapshotLiveTurnHtmlForSession(activeSid);
  }
  // Throttled variant for token-by-token updates. persistInflightState()
  // calls saveInflightState() which does JSON.parse + JSON.stringify + write
  // on the entire inflight map every call. On a fast model at 60 tok/s with
  // a 10KB messages array this is ~36MB of JSON churn per second — a major
  // GC pressure source that causes the renderer to crash under load.
  // State transitions (tool events, done, error) still call persistInflightState()
  // directly so no more than 2s of progress is lost on a crash.
  let _persistTimer=null;
  function _throttledPersist(){
    if(_persistTimer) return;
    _persistTimer=setTimeout(()=>{_persistTimer=null;persistInflightState();},2000);
  }
  function _closeSource(source){
    closeLiveStream(activeSid, streamId, source);
  }
  function _clearStreamEndRecovery(){
    if(_streamEndRecoveryTimer){
      clearTimeout(_streamEndRecoveryTimer);
      _streamEndRecoveryTimer=null;
    }
    _pendingStreamEndRecovery=false;
    _streamEndRecoveryAttempts=0;
  }
  function _liveStreamEndScenePresent(){
    if(assistantText||assistantRow) return true;
    if(String(liveReasoningText||reasoningText||'').trim()) return true;
    const inflight=INFLIGHT[activeSid];
    if(inflight&&Array.isArray(inflight.toolCalls)&&inflight.toolCalls.length) return true;
    if(!_isActiveSession()||typeof document==='undefined') return false;
    const turn=$('liveAssistantTurn');
    return !!(turn&&turn.querySelector(
      '[data-live-assistant="1"],'+
      '.live-worklog[data-live-worklog-shell="1"],'+
      '.tool-card-row[data-live-tid],'+
      '.agent-activity-thinking[data-thinking-active="1"]'
    ));
  }
  function _scheduleStreamEndRecovery(source, delay=180){
    if(_streamEndRecoveryTimer) clearTimeout(_streamEndRecoveryTimer);
    _pendingStreamEndRecovery=true;
    _streamEndRecoveryTimer=setTimeout(()=>{void _runStreamEndRecovery(source);},delay);
  }
  function _finalizeStreamEndFallback(source){
    _clearStreamEndRecovery();
    if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
    _terminalStateReached=true;
    _streamFinalized=true;
    _cancelAnimationFramePendingStreamRender();
    _streamFadeCleanupReduceMotionListener();
    _smdEndParser();
    if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
    _clearOwnerInflightState();
    _clearStreamHidden(activeSid, streamId);  // #4416: terminal path, drop hidden tracker
    _flushReasoningToAnchor();
    _scheduleAnchorRegistryCleanup();
    _clearApprovalForOwner();
    _clearClarifyForOwner('terminal');
    if(_isActiveSession()){
      S.activeStreamId=null;
      clearLiveToolCards();if(!assistantText)removeThinking();
      renderMessages({preserveScroll:true});
    }
    renderSessionList();
    _setActivePaneIdleIfOwner();
    _closeSource(source);
  }
  async function _runStreamEndRecovery(source){
    if(_streamFinalized || _terminalStateReached || !_pendingStreamEndRecovery){
      _clearStreamEndRecovery();
      return;
    }
    _streamEndRecoveryTimer=null;
    const status=await _restoreSettledSession(source,{status:true});
    if(status==='restored'){
      _clearStreamEndRecovery();
      return;
    }
    if(status==='active'&&_streamEndRecoveryAttempts<10){
      _streamEndRecoveryAttempts+=1;
      _scheduleStreamEndRecovery(source,200);
      return;
    }
    _finalizeStreamEndFallback(source);
  }
  function _stripLiveVisibleAssistantEchoFromThinking(text, snippets){
    let out=String(text||'');
    (Array.isArray(snippets)?snippets:[]).forEach(snippet=>{
      const visible=String(snippet||'').trim();
      if(visible.length<20) return;
      out=out.split(visible).join('');
    });
    return out.trim();
  }
  function _liveThinkingText(){
    return String(liveReasoningText||'').trim() || 'Thinking…';
  }
  function _liveThinkingPlacement(){
    const activeSeq=Number(_assistantSegmentSeq||0);
    const nextSeq=Number(_currentLiveSegmentSeq||0)+1;
    const segmentSeq=(!assistantRow||_freshSegment||!activeSeq)?nextSeq:activeSeq;
    return {
      activityKey:S.activeStreamId?'live:'+S.activeStreamId:null,
      segmentSeq,
      burstId:_currentActivityBurstId,
    };
  }
  function _updateLiveThinkingCard(text){
    const opts=_liveThinkingPlacement();
    if(typeof updateThinking==='function') updateThinking(text, opts);
    else appendThinking(text, opts);
  }
  // Split a content string into {reasoning, content} by extracting any <think>...
  // blocks (or other known reasoning-tag pairs). If reasoning is already
  // populated on the message (e.g. from a separate on_reasoning stream), the
  // inline blocks are stripped but the existing reasoning field is preserved.
  // Provider-bug workaround: M3 (and similar reasoning models) emit the
  // thinking inline in the OpenAI-compat content stream instead of a separate
  // reasoning channel, which would otherwise bloat the persisted session
  // message by 30-50% and miss the m.reasoning field used by the thinking card.
  function _splitThinkFromContent(rawContent, existingReasoning){
    return _extractInlineThinkingFromContent(rawContent, existingReasoning, {streaming:false});
  }
  function syncInflightAssistantMessage(){
    const inflight=INFLIGHT[activeSid];
    if(!inflight) return;
    inflight.lastAssistantText=assistantText;
    inflight.lastReasoningText=reasoningText;
    if(!Array.isArray(inflight.messages)) inflight.messages=[];
    let assistantIdx=-1;
    for(let i=inflight.messages.length-1;i>=0;i--){
      const msg=inflight.messages[i];
      if(msg&&msg.role==='assistant'&&msg._live){assistantIdx=i;break;}
    }
    const ts=Date.now()/1000;
    // Split inline <think> blocks into m.reasoning so the persisted inflight
    // state stays compact and the thinking card has a proper source field.
    const split=_splitThinkFromContent(assistantText, reasoningText);
    if(assistantIdx>=0){
      inflight.messages[assistantIdx].content=split.content;
      inflight.messages[assistantIdx].reasoning=split.reasoning||undefined;
      inflight.messages[assistantIdx]._ts=inflight.messages[assistantIdx]._ts||ts;
      _throttledPersist();
      return;
    }
    inflight.messages.push({role:'assistant',content:split.content,reasoning:split.reasoning||undefined,_live:true,_ts:ts});
    _throttledPersist();
  }
  function recordActivityBoundary(){
    const inflight=INFLIGHT[activeSid];
    if(!inflight) return;
    if(!Array.isArray(inflight.activityBurstAnchors)) inflight.activityBurstAnchors=[];
    if(!assistantRow||!assistantRow.isConnected){
      assistantRow=null;
      assistantBody=null;
    }
    const textEnd=String(assistantText||'').length;
    const lastTextEnd=inflight.activityBurstAnchors.reduce((max,a)=>{
      const n=Number(a&&a.textEnd);
      return Number.isFinite(n)?Math.max(max,n):max;
    },0);
    if(textEnd<=lastTextEnd){
      inflight.currentActivityBurstId=_currentActivityBurstId;
      if(assistantRow) assistantRow.setAttribute('data-activity-burst-id',String(_currentActivityBurstId));
      persistInflightState();
      return;
    }
    _currentActivityBurstId+=1;
    inflight.currentActivityBurstId=_currentActivityBurstId;
    const existing=inflight.activityBurstAnchors.find(a=>Number(a&&a.id)===_currentActivityBurstId);
    if(existing) existing.textEnd=textEnd;
    else inflight.activityBurstAnchors.push({id:_currentActivityBurstId,textEnd});
    if(assistantRow) assistantRow.setAttribute('data-activity-burst-id',String(_currentActivityBurstId));
    persistInflightState();
  }
  function ensureAssistantRow(force=false){
    if(!_isActiveSession()) return;
    if(assistantRow&&!assistantRow.isConnected){assistantRow=null;assistantBody=null;}
    if(!force&&!assistantRow){
      const parsed=_parseStreamState();
      if(!String((parsed&&parsed.displayText)||'').trim()) return;
    }
    let turn=$('liveAssistantTurn');
    if(!turn){
      appendThinking();
      turn=$('liveAssistantTurn');
    }
    const blocks=(typeof _assistantTurnBlocks==='function')?_assistantTurnBlocks(turn):null;
    if(!blocks) return;
    if(!assistantRow){
      // After a tool call _freshSegment=true, so we always create a new segment
      // below the tool card rather than re-attaching to the old one above it.
      if(!_freshSegment){
        const liveSegments=blocks.querySelectorAll('[data-live-assistant="1"]');
        const existing=liveSegments.length?liveSegments[liveSegments.length-1]:null;
        if(existing){
          assistantRow=existing;
          assistantBody=existing.querySelector('.msg-body');
          const existingSeq=Number(existing.getAttribute('data-live-segment-seq')||'');
          if(Number.isFinite(existingSeq)&&existingSeq>0){
            _assistantSegmentSeq=existingSeq;
            if(_assistantSegmentSeq>_currentLiveSegmentSeq) _currentLiveSegmentSeq=_assistantSegmentSeq;
          }
        }
      }
    }
    if(assistantRow){
      if(typeof placeLiveToolCardsHost==='function') placeLiveToolCardsHost();
      if(typeof _moveLiveRunStatusToTurnEnd==='function') _moveLiveRunStatusToTurnEnd();
      return;
    }

    const tr=$('toolRunningRow');if(tr)tr.remove();
    $('emptyState').style.display='none';
    assistantRow=document.createElement('div');
    assistantRow.className='assistant-segment';
    _currentLiveSegmentSeq+=1;
    _assistantSegmentSeq=_currentLiveSegmentSeq;
    assistantRow.setAttribute('data-live-assistant','1');
    assistantRow.setAttribute('data-activity-burst-id',String(_currentActivityBurstId));
    assistantRow.setAttribute('data-live-segment-seq',String(_assistantSegmentSeq));
    assistantBody=document.createElement('div');assistantBody.className='msg-body';
    assistantRow.appendChild(assistantBody);
    blocks.appendChild(assistantRow);
    if(typeof _moveLiveRunStatusToTurnEnd==='function') _moveLiveRunStatusToTurnEnd();
    if(INFLIGHT[activeSid]){
      INFLIGHT[activeSid].currentLiveSegmentSeq=_currentLiveSegmentSeq;
    }
    _freshSegment=false; // consumed — next reuse check is normal again
  }

  // ── Shared SSE handler wiring (used for initial connection and reconnect) ──
  let _reconnectAttempted=false;
  let _terminalStateReached=false;
  let _deferredStreamRecoveryBound=false;
  let _pendingStreamEndRecovery=false;
  let _streamEndRecoveryTimer=null;
  let _streamEndRecoveryAttempts=0;

  function _pageHiddenForStreamError(){
    return (typeof document!=='undefined'&&document.visibilityState==='hidden')||
      (typeof document!=='undefined'&&document.wasDiscarded===true);
  }

  function _reattachOrRestoreAfterDeferredStreamError(source){
    if(_terminalStateReached||_streamFinalized) return;
    if((S.session&&S.session.session_id)!==activeSid) return;
    (async()=>{
      try{
        if(streamId){
          const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
          if(st.active){
            setComposerStatus('Reconnected');
            _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}${_runJournalReplayParams()}`,document.baseURI||location.href).href,{withCredentials:true}));
            return;
          }
        }
      }catch(_){
        if(_deferStreamErrorIfOffline()||_pageHiddenForStreamError()) return;
      }
      if(await _restoreSettledSession(source)) return;
      if(_deferStreamErrorIfOffline()||_pageHiddenForStreamError()) return;
      _flushReasoningToAnchor();
      _scheduleAnchorRegistryCleanup(120000);
      _handleStreamError(source);
    })();
  }

  function _deferStreamErrorIfPageHidden(source){
    if(!_pageHiddenForStreamError()) return false;
    setComposerStatus('Connection paused. Reconnecting when this tab returns…');
    if(S.session&&S.session.session_id===activeSid&&streamId) S.activeStreamId=streamId;
    if(!_deferredStreamRecoveryBound){
      _deferredStreamRecoveryBound=true;
      const resume=()=>{
        if(_pageHiddenForStreamError()) return;
        window.removeEventListener('focus',resume);
        window.removeEventListener('pageshow',resume);
        document.removeEventListener('visibilitychange',resume);
        _deferredStreamRecoveryBound=false;
        _reattachOrRestoreAfterDeferredStreamError(source);
      };
      document.addEventListener('visibilitychange',resume);
      window.addEventListener('focus',resume);
      window.addEventListener('pageshow',resume);
    }
    return true;
  }

  // Bug A fix (#631): track whether the stream has been finalized so any rAF
  // scheduled by a trailing 'token'/'reasoning' event that arrives in the same
  // microtask batch as 'done' does not fire after renderMessages() has already
  // settled the DOM — which was causing the thinking card to reappear below
  // the final answer or the response to render twice.
  let _streamFinalized=false;
  let _pendingRafHandle=null;
  let _streamFadeVisibleText='';
  let _streamFadeLastTickMs=0;
  let _streamFadeWordCarry=0;
  let _streamFadeStartedAt=0;
  let _streamFadeLastTargetWords=0;
  let _streamFadeLastArrivalMs=0;
  let _streamFadeArrivalWps=0;
  let _streamFadeLatestAnimationEndAt=0;
  let _streamFadeAppendOffset=0;
  let _streamFadeVisibleWords=0;
  let _streamFadeHoldUntilMs=0;
  let _streamFadeCurrentMs=200;
  let _streamFadeReduceMotionMql=null;
  let _streamFadeReduceMotion=false;
  let _streamFadeReduceMotionOnChange=null;
  let _currentActivityBurstId=Number((INFLIGHT[activeSid]&&INFLIGHT[activeSid].currentActivityBurstId)||0)||0;
  let _currentLiveSegmentSeq=Number((INFLIGHT[activeSid]&&INFLIGHT[activeSid].currentLiveSegmentSeq)||0)||0;
  let _assistantSegmentSeq=Number((INFLIGHT[activeSid]&&INFLIGHT[activeSid].currentLiveSegmentSeq)||0)||0;
  let _lastRunJournalSeq=reconnecting
    ? Number((INFLIGHT[activeSid]&&INFLIGHT[activeSid].lastRunJournalSeq)||0)
    : 0;
  let _lastRunJournalEventId='';
  const _STREAM_FADE_MS=200;
  const _STREAM_FADE_MAX_MS=350;
  const _STREAM_FADE_STAGGER_MS=16;
  const _STREAM_FADE_DONE_MAX_MS=320;
  const _STREAM_FADE_DONE_DRAIN_MAX_MS=900;
  const _anchorApi=(typeof window!=='undefined'&&window.HermesAssistantTurnAnchors)
    ? window.HermesAssistantTurnAnchors
    : null;
  const _anchorRegistryMap=(typeof window!=='undefined')
    ? (window._liveAnchorRegistries=window._liveAnchorRegistries||new Map())
    : null;
  const _existingAnchorRegistry=_anchorRegistryMap?_anchorRegistryMap.get(streamId):null;
  const _anchorRegistry=_existingAnchorRegistry||(_anchorApi&&typeof _anchorApi.createAssistantTurnAnchorRegistry==='function'
    ? _anchorApi.createAssistantTurnAnchorRegistry({
      session_id:activeSid,
      stream_id:streamId,
      run_id:null,
    })
    : null);
  let _anchorShadowWarned=false;
  let _anchorReasoningFlushed=false;
  let _anchorLocalSeq=0;
  if(_anchorRegistryMap&&_anchorRegistry) _anchorRegistryMap.set(streamId,_anchorRegistry);
  function _scheduleAnchorRegistryCleanup(delayMs=600000){
    if(!_anchorRegistryMap||!_anchorRegistry) return;
    setTimeout(()=>{
      if(_anchorRegistryMap.get(streamId)===_anchorRegistry) _anchorRegistryMap.delete(streamId);
    },delayMs);
  }
  // Backstop: schedule an identity-guarded cleanup at creation so this shadow
  // registry self-expires no matter which teardown path the stream takes
  // (incl. external ones like sidebar cancelSessionStream() that bypass the
  // in-closure SSE handlers). Explicit terminal-path calls above just expire it
  // sooner; this guarantees window._liveAnchorRegistries can't grow unbounded.
  _scheduleAnchorRegistryCleanup(600000);
  function _applyToAnchor(sourceEventType, rawEventData, sseEvent){
    if(!_anchorRegistry||!_anchorApi||typeof _anchorApi.applyAssistantTurnAnchorSourceEvent!=='function') return null;
    const raw=(rawEventData&&typeof rawEventData==='object')?rawEventData:{};
    const eventId=(sseEvent&&sseEvent.lastEventId)||raw.event_id||raw.lastEventId||raw.last_event_id||'';
    const sourceEvent={
      ...raw,
      source_event_type:sourceEventType,
      activitySegmentSeq:raw.activitySegmentSeq??raw.activity_segment_seq??_assistantSegmentSeq,
      activityBurstId:raw.activityBurstId??raw.activity_burst_id??_currentActivityBurstId,
    };
    if(eventId) sourceEvent.event_id=eventId;
    try{
      const result=_anchorApi.applyAssistantTurnAnchorSourceEvent(
        _anchorRegistry,
        sourceEvent,
        {session_id:activeSid,stream_id:streamId}
      );
      _renderAnchorLiveScene();
      return result;
    }catch(err){
      if(!_anchorShadowWarned&&typeof console!=='undefined'&&console.warn){
        _anchorShadowWarned=true;
        console.warn('assistant turn anchor live shadow feed failed',err);
      }
      return null;
    }
  }
  function _anchorActivityEvents(){
    const anchor=_anchorRegistry&&_anchorRegistry.anchor;
    return anchor&&Array.isArray(anchor.activity_events)?anchor.activity_events:null;
  }
  function _findAnchorActivityEventByLocalId(localId, sourceEventType){
    const events=_anchorActivityEvents();
    if(!events||!localId) return null;
    for(let i=events.length-1;i>=0;i--){
      const event=events[i];
      if(!event||event.local_id!==localId) continue;
      if(sourceEventType&&event.source_event_type!==sourceEventType) continue;
      return event;
    }
    return null;
  }
  function _latestAnchorCompressionEventIndex(sourceEventType){
    const events=_anchorActivityEvents();
    if(!events) return -1;
    for(let i=events.length-1;i>=0;i--){
      const event=events[i];
      if(event&&event.source_event_type===sourceEventType) return i;
    }
    return -1;
  }
  function _anchorCompressionCompletedAfter(index){
    const events=_anchorActivityEvents();
    if(!events) return false;
    for(let i=events.length-1;i>index;i--){
      const event=events[i];
      if(event&&event.source_event_type==='compressed') return true;
    }
    return false;
  }
  function _ensureAnchorCompressionCompletedOnLiveProgress(sessionId){
    if(!_anchorRegistry||!_anchorApi) return false;
    const sid=String(sessionId||activeSid||'');
    const events=_anchorActivityEvents();
    const runningIndex=_latestAnchorCompressionEventIndex('compressing');
    if(runningIndex>=0&&_anchorCompressionCompletedAfter(runningIndex)) return true;
    const runningEvent=(events&&runningIndex>=0)?events[runningIndex]:null;
    const basis=String((runningEvent&&(runningEvent.local_id||runningEvent.event_id))||streamId||sid||'compression');
    const localId=`live-compression-complete:${basis}`;
    if(_findAnchorActivityEventByLocalId(localId,'compressed')) return true;
    const eventId=`synthetic:${localId}`;
    const result=_applyToAnchor('compressed',{
      event_id:eventId,
      local_id:localId,
      session_id:sid,
      old_session_id:sid,
      automatic:true,
      synthetic:true,
      status:'completed',
      phase:'done',
      message:'Context auto-compressed',
    },{lastEventId:eventId});
    return !!(result&&(result.applied||result.reason==='duplicate'));
  }
  function _replaceAnchorActivityEventByLocalId(localId, sourceEventType, patch){
    const events=_anchorActivityEvents();
    if(!events||!localId) return null;
    for(let i=events.length-1;i>=0;i--){
      const event=events[i];
      if(!event||event.local_id!==localId) continue;
      if(sourceEventType&&event.source_event_type!==sourceEventType) continue;
      const next={
        ...event,
        ...(patch||{}),
        payload:{
          ...(event.payload&&typeof event.payload==='object'?event.payload:{}),
          ...((patch&&patch.payload&&typeof patch.payload==='object')?patch.payload:{}),
        },
      };
      events[i]=next;
      return next;
    }
    return null;
  }
  function _nextAnchorLocalSeq(){
    _anchorLocalSeq+=1;
    const cursor=Number(_runJournalReplayAfterSeq&&_runJournalReplayAfterSeq());
    return (Number.isFinite(cursor)?cursor:0)+_anchorLocalSeq;
  }
  function _anchorSegmentSeq(){
    const seq=Number(_assistantSegmentSeq||_currentLiveSegmentSeq||0);
    return Number.isFinite(seq)&&seq>0?seq:1;
  }
  function _renderAnchorLiveScene(){
    if(!_anchorRegistry||!_isActiveSession()) return false;
    if(typeof window==='undefined'||typeof window._renderLiveAnchorActivitySceneForStream!=='function') return false;
    try{
      return !!window._renderLiveAnchorActivitySceneForStream(streamId, activeSid, {
        mode:'compact_worklog',
      });
    }catch(err){
      if(!_anchorShadowWarned&&typeof console!=='undefined'&&console.warn){
        _anchorShadowWarned=true;
        console.warn('assistant turn anchor live scene render failed',err);
      }
      return false;
    }
  }
  function _projectLiveAnchorActivityScene(){
    if(!_anchorRegistry||!_anchorApi||typeof _anchorApi.projectAssistantTurnAnchorActivityScene!=='function') return null;
    try{
      return _anchorApi.projectAssistantTurnAnchorActivityScene(_anchorRegistry,{mode:'compact_worklog'});
    }catch(_){
      return null;
    }
  }
  function _anchorSceneMessageRef(message){
    if(!message||typeof message!=='object') return '';
    let content=message.content||'';
    if(Array.isArray(content)){
      try{
        content=content.map(part=>{
          if(part&&typeof part==='object') return part.text||part.content||part.input_text||'';
          return String(part||'');
        }).join('\n');
      }catch(_){ content=''; }
    }
    const payload={
      role:String(message.role||''),
      content:String(content||'').replace(/\s+/g,' ').trim(),
      timestamp:message._ts||message.timestamp||'',
    };
    return JSON.stringify(payload);
  }
  function _anchorSceneMessageText(message){
    if(!message||typeof message!=='object') return '';
    let content=message.content||'';
    if(Array.isArray(content)){
      try{
        content=content.map(part=>{
          if(part&&typeof part==='object') return part.text||part.content||part.input_text||'';
          return String(part||'');
        }).join('\n');
      }catch(_){ content=''; }
    }
    return typeof content==='string'?content:String(content||'');
  }
  function _anchorSceneCleanText(value){
    return String(value||'').replace(/\s+/g,' ').trim();
  }
  function _anchorSceneTextKey(value){
    return _anchorSceneCleanText(value).toLowerCase();
  }
  function _anchorSceneSafePayload(value){
    if(value===undefined) return undefined;
    if(value===null||typeof value!=='object') return value;
    try{
      return JSON.parse(JSON.stringify(value));
    }catch(_){
      return String(value);
    }
  }
  function _anchorSceneToolId(tool){
    return String(tool&&(tool.tid||tool.id||tool.tool_call_id||tool.tool_use_id||tool.call_id)||'').trim();
  }
  function _anchorSceneToolName(tool){
    const fn=tool&&tool.function&&typeof tool.function==='object'?tool.function:{};
    return String(tool&&(tool.name||tool.tool_name)||fn.name||'tool').trim()||'tool';
  }
  function _anchorSceneToolArgs(tool){
    if(!tool||typeof tool!=='object') return {};
    if(tool.args&&typeof tool.args==='object') return _anchorSceneSafePayload(tool.args)||{};
    if(tool.input&&typeof tool.input==='object') return _anchorSceneSafePayload(tool.input)||{};
    const fn=tool.function&&typeof tool.function==='object'?tool.function:{};
    if(typeof fn.arguments==='string'&&fn.arguments.trim()){
      try{
        const parsed=JSON.parse(fn.arguments);
        return parsed&&typeof parsed==='object'?_anchorSceneSafePayload(parsed):{};
      }catch(_){}
    }
    return {};
  }
  function _anchorSceneStringPayload(value){
    if(value===undefined||value===null) return '';
    if(typeof value==='string') return value;
    try{
      return JSON.stringify(value);
    }catch(_){
      return String(value);
    }
  }
  function _anchorSceneRowBase(role, kind, sourceEventType, orderIndex, messageIndex){
    const groupKey=Number.isFinite(Number(messageIndex))?`assistant:${Number(messageIndex)}`:`activity:${orderIndex}`;
    return {
      row_id:`settled:${activeSid||'session'}:${streamId||'stream'}:${role}:${messageIndex}:${orderIndex}`,
      order_index:orderIndex,
      kind,
      role,
      display_hint:role==='prose'?'main_prose':role==='thinking'?'collapsed_thinking':role==='tool'?'tool_row':role==='terminal'?'terminal_status_row':'activity_row',
      display_hints:{
        compact_worklog:role==='prose'?'main_prose':role==='thinking'?'collapsed_thinking':role==='tool'?'tool_row':role==='terminal'?'terminal_status_row':'activity_row',
        transparent_stream:'chronological_activity',
      },
      source_event_type:sourceEventType,
      event_id:null,
      local_id:null,
      run_id:null,
      stream_id:streamId||null,
      seq:orderIndex,
      status:role==='terminal'?'completed':'completed',
      created_at:null,
      identity:{event_id:null,local_id:null,run_id:null,stream_id:streamId||null,seq:orderIndex},
      group:{
        group_key:groupKey,
        activity_burst_id:null,
        activity_segment_seq:null,
        assistant_msg_idx:Number.isFinite(Number(messageIndex))?Number(messageIndex):null,
      },
      text:'',
      thinking:null,
      tool_call_id:null,
      tool:null,
      payload:{assistant_msg_idx:Number.isFinite(Number(messageIndex))?Number(messageIndex):null},
    };
  }
  function _anchorSceneProseRow(text, orderIndex, messageIndex){
    const row=_anchorSceneRowBase('prose','process_prose','settled_message',orderIndex,messageIndex);
    row.text=String(text||'');
    row.payload={...row.payload,text:row.text};
    return row;
  }
  function _anchorSceneThinkingRow(text, orderIndex, messageIndex){
    const row=_anchorSceneRowBase('thinking','reasoning','reasoning',orderIndex,messageIndex);
    row.text=String(text||'');
    const preview=_anchorSceneCleanText(text);
    row.thinking={
      text:row.text,
      preview:preview.length>180?`${preview.slice(0,177)}...`:preview,
      dedupe_key:preview?`thinking:${preview.toLowerCase()}`:'',
    };
    row.payload={...row.payload,text:row.text};
    return row;
  }
  function _anchorSceneToolRowFromCall(tool, orderIndex, messageIndex){
    const row=_anchorSceneRowBase('tool','tool_completed','tool_complete',orderIndex,messageIndex);
    const tid=_anchorSceneToolId(tool);
    const name=_anchorSceneToolName(tool);
    const args=_anchorSceneToolArgs(tool);
    const command=_anchorSceneStringPayload(tool&&(tool.command||tool.raw_command||tool.original_command||tool.display_command))||_anchorSceneStringPayload(args&&(args.cmd||args.command));
    const preview=_anchorSceneStringPayload(tool&&(tool.preview||tool.summary));
    const snippet=_anchorSceneStringPayload(tool&&(tool.snippet||tool.result||tool.output));
    const isError=!!(tool&&(tool.is_error||tool.error));
    row.row_id=tid?`settled:${activeSid||'session'}:${streamId||'stream'}:tool:${tid}`:row.row_id;
    row.tool_call_id=tid||null;
    row.tool={
      id:tid||null,
      name,
      args,
      command,
      preview,
      snippet,
      result:_anchorSceneSafePayload(tool&&tool.result)??null,
      output:_anchorSceneSafePayload(tool&&tool.output)??null,
      done:true,
      is_error:isError,
      duration:tool&&tool.duration!==undefined?tool.duration:null,
      started_at:tool&&tool.started_at!==undefined?tool.started_at:null,
      signature:[name,tid||'',JSON.stringify(args||{})].join('|'),
    };
    row.payload={
      ...row.payload,
      tid:tid||undefined,
      id:tid||undefined,
      name,
      args,
      command,
      preview,
      snippet,
      is_error:isError,
      duration:tool&&tool.duration!==undefined?tool.duration:undefined,
      started_at:tool&&tool.started_at!==undefined?tool.started_at:undefined,
    };
    return row;
  }
  function _anchorSceneMessageReasoningText(message){
    if(!message||typeof message!=='object') return '';
    return String(message.reasoning||message._reasoning||message.reasoning_content||message.thinking||'');
  }
  function _anchorSceneRowsByMessageIndex(messages, turnStart, lastAsstIndex){
    const byIdx=new Map();
    const add=(idx,row)=>{
      if(!byIdx.has(idx)) byIdx.set(idx,[]);
      byIdx.get(idx).push(row);
    };
    let order=0;
    for(let idx=turnStart+1;idx<lastAsstIndex;idx+=1){
      const message=messages[idx];
      if(!message||message.role!=='assistant') continue;
      const text=_anchorSceneMessageText(message);
      if(_anchorSceneCleanText(text)) add(idx,_anchorSceneProseRow(text,order++,idx));
      const reasoning=_anchorSceneMessageReasoningText(message);
      if(_anchorSceneCleanText(reasoning)&&_anchorSceneTextKey(reasoning)!==_anchorSceneTextKey(text)){
        add(idx,_anchorSceneThinkingRow(reasoning,order++,idx));
      }
      const messageTools=[];
      if(Array.isArray(message.tool_calls)) messageTools.push(...message.tool_calls);
      if(Array.isArray(message._partial_tool_calls)) messageTools.push(...message._partial_tool_calls);
      for(const tool of messageTools){
        add(idx,_anchorSceneToolRowFromCall(tool,order++,idx));
      }
    }
    const toolCalls=Array.isArray(S.toolCalls)?S.toolCalls:[];
    for(const tool of toolCalls){
      if(!tool||typeof tool!=='object') continue;
      const idx=Number(tool.assistant_msg_idx);
      if(!Number.isFinite(idx)||idx<=turnStart||idx>=lastAsstIndex) continue;
      add(idx,_anchorSceneToolRowFromCall(tool,order++,idx));
    }
    return byIdx;
  }
  function _anchorSceneExistingRowKey(row){
    if(!row||typeof row!=='object') return '';
    if(row.role==='tool'){
      const tool=row.tool&&typeof row.tool==='object'?row.tool:{};
      return `tool:${row.tool_call_id||tool.id||tool.tid||tool.tool_call_id||tool.tool_use_id||tool.call_id||row.row_id||''}`;
    }
    if(row.role==='prose'||row.role==='thinking') return `${row.role}:${_anchorSceneTextKey(row.text)}`;
    return `${row.role||row.kind}:${row.source_event_type||''}:${row.status||''}:${row.row_id||''}`;
  }
  function _anchorSceneRowHasLiveIdentity(row){
    if(!row||typeof row!=='object') return false;
    const identity=row.identity&&typeof row.identity==='object'?row.identity:{};
    const values=[row.row_id,row.local_id,row.event_id,identity.local_id,identity.event_id];
    return values.some(value=>String(value||'').startsWith('live-'));
  }
  function _anchorSceneMessageRowsHaveThinking(messageRows){
    if(!(messageRows instanceof Map)) return false;
    for(const bucket of messageRows.values()){
      if(Array.isArray(bucket)&&bucket.some(row=>row&&row.role==='thinking')) return true;
    }
    return false;
  }
  function _anchorSceneSettleLiveRunningRow(row, hasSettledThinking){
    if(!row||typeof row!=='object') return row;
    if(row.role!=='thinking'&&row.role!=='prose'&&row.role!=='tool') return row;
    if(String(row.status||'').toLowerCase()!=='running') return row;
    if(!_anchorSceneRowHasLiveIdentity(row)) return row;
    if(row.role==='thinking'&&hasSettledThinking) return null;
    return {...row,status:'completed'};
  }
  function _anchorSceneRowLooksLikeFinalAnswer(rowTextKey, finalKey){
    if(!rowTextKey||!finalKey) return false;
    if(rowTextKey===finalKey) return true;
    // #4587: align with the renderer's _anchorSceneProseMatchesFinalAnswer — a
    // prefix-like overlap only counts as "the final answer" (and is dropped from
    // the scene) when it's a NEAR-complete match (ratio>=0.9). A shorter
    // intermediate-prose row that merely happens to be a prefix of the final
    // answer is legitimate progress narration and must be PRESERVED, not dropped.
    if(!(finalKey.startsWith(rowTextKey)||rowTextKey.startsWith(finalKey))) return false;
    const shorter=Math.min(rowTextKey.length,finalKey.length);
    const longer=Math.max(rowTextKey.length,finalKey.length);
    return shorter>=80&&longer>0&&(shorter/longer)>=0.9;
  }
  function _anchorSceneRowTextOverlapsExisting(rowTextKey, seenTextKeys){
    if(!rowTextKey||!Array.isArray(seenTextKeys)) return false;
    for(const existing of seenTextKeys){
      if(!existing) continue;
      if(rowTextKey===existing) return true;
      const minLen=Math.min(rowTextKey.length,existing.length);
      if(minLen>=80&&(rowTextKey.includes(existing)||existing.includes(rowTextKey))) return true;
    }
    return false;
  }
  function _anchorSceneTurnDurationForSettlement(lastAsst, base){
    if(lastAsst&&lastAsst._turnDuration!==undefined&&lastAsst._turnDuration!==null) return lastAsst._turnDuration;
    if(base&&base.turn_duration!==undefined&&base.turn_duration!==null) return base.turn_duration;
    const session=(typeof S!=='undefined'&&S&&S.session)?S.session:null;
    const candidates=[
      session&&session.pending_started_at,
      session&&session.active_started_at,
      session&&session.run_started_at,
      session&&session.started_at,
    ];
    for(const raw of candidates){
      const started=Number(raw);
      if(Number.isFinite(started)&&started>0){
        const elapsed=(Date.now()/1000)-started;
        if(Number.isFinite(elapsed)&&elapsed>=0) return elapsed;
      }
    }
    return undefined;
  }
  function _completeSettledAnchorSceneForTurn(messages, lastAsstIndex, projectedScene){
    if(!Array.isArray(messages)||lastAsstIndex<0) return projectedScene;
    const lastAsst=messages[lastAsstIndex];
    if(!lastAsst||lastAsst.role!=='assistant') return projectedScene;
    let turnStart=-1;
    for(let idx=lastAsstIndex-1;idx>=0;idx-=1){
      if(messages[idx]&&messages[idx].role==='user'){
        turnStart=idx;
        break;
      }
    }
    const finalAnswer=_anchorSceneMessageText(lastAsst);
    const finalKey=_anchorSceneTextKey(finalAnswer);
    const base=(projectedScene&&typeof projectedScene==='object')?projectedScene:{};
    const messageRows=_anchorSceneRowsByMessageIndex(messages,turnStart,lastAsstIndex);
    const hasSettledThinking=_anchorSceneMessageRowsHaveThinking(messageRows);
    const rows=[];
    const seen=new Set();
    const seenTextKeys=[];
    const pushRow=(row)=>{
      if(!row||typeof row!=='object') return;
      row=_anchorSceneSettleLiveRunningRow(row,hasSettledThinking);
      if(!row||typeof row!=='object') return;
      const textKey=_anchorSceneTextKey(row.text);
      const isTextual=row.role==='prose'||row.role==='thinking';
      if(isTextual&&_anchorSceneRowLooksLikeFinalAnswer(textKey,finalKey)) return;
      if(isTextual&&_anchorSceneRowTextOverlapsExisting(textKey,seenTextKeys)) return;
      const key=_anchorSceneExistingRowKey(row);
      if(key&&seen.has(key)) return;
      if(key) seen.add(key);
      if(isTextual&&textKey) seenTextKeys.push(textKey);
      rows.push({...row,order_index:rows.length,seq:rows.length});
    };
    const projectedRows=Array.isArray(base.activity_rows)?base.activity_rows:[];
    for(const row of projectedRows){
      if(row&&row.role==='terminal') continue;
      pushRow(row);
    }
    for(let idx=turnStart+1;idx<lastAsstIndex;idx+=1){
      const bucket=messageRows.get(idx)||[];
      for(const row of bucket) pushRow(row);
    }
    for(const row of projectedRows){
      if(row&&row.role==='terminal') pushRow(row);
    }
    const scene={
      ...base,
      version:'activity_scene_v1',
      mode:'compact_worklog',
      identity:{
        ...((base.identity&&typeof base.identity==='object')?base.identity:{}),
        source_message_refs:messages.slice(turnStart+1,lastAsstIndex+1)
          .filter(m=>m&&m.role==='assistant')
          .map(m=>_anchorSceneMessageRef(m)),
      },
      lifecycle:(base.lifecycle&&typeof base.lifecycle==='object')?{...base.lifecycle}:{},
      final_answer:_anchorSceneCleanText(finalAnswer)?finalAnswer:(typeof base.final_answer==='string'?base.final_answer:''),
      final_message_ref:_anchorSceneMessageRef(lastAsst),
      turn_duration:_anchorSceneTurnDurationForSettlement(lastAsst,base),
      terminal_state:base.terminal_state||((base.lifecycle&&base.lifecycle.terminal_state)||null),
      activity_rows:rows,
    };
    return scene;
  }
  let _persistAnchorSceneWarned=false;
  function _anchorSceneMessageOffsetForPersist(){
    const raw=(typeof _oldestIdx!=='undefined')?_oldestIdx:0;
    const offset=Number(raw);
    return Number.isFinite(offset)&&offset>0?Math.floor(offset):0;
  }
  function _anchorSceneAbsoluteMessageIndexForPersist(messageIndex, offset){
    const idx=Number(messageIndex);
    const off=Number(offset);
    if(!Number.isFinite(idx)||idx<0) return messageIndex;
    return idx+(Number.isFinite(off)&&off>0?Math.floor(off):0);
  }
  function _persistSettledAnchorScene(message, scene, messageIndex){
    if(!activeSid||!message||!scene||typeof api!=='function') return;
    try{
      const messageOffset=_anchorSceneMessageOffsetForPersist();
      api('/api/session/anchor-scene',{
        method:'POST',
        timeoutMs:8000,
        timeoutToast:false,
        body:JSON.stringify({
          session_id:activeSid,
          stream_id:streamId,
          message_index:_anchorSceneAbsoluteMessageIndexForPersist(messageIndex,messageOffset),
          message_window_index:messageIndex,
          message_offset:messageOffset,
          message_ref:_anchorSceneMessageRef(message),
          scene,
        }),
      }).catch(err=>{
        if(!_persistAnchorSceneWarned&&typeof console!=='undefined'&&console.warn){
          _persistAnchorSceneWarned=true;
          console.warn('anchor activity scene persistence failed',err);
        }
      });
    }catch(err){
      if(!_persistAnchorSceneWarned&&typeof console!=='undefined'&&console.warn){
        _persistAnchorSceneWarned=true;
        console.warn('anchor activity scene persistence failed',err);
      }
    }
  }
  function _attachProjectedAnchorSceneToLastAssistant(messages){
    if(!_anchorRegistry||!Array.isArray(messages)) return false;
    let lastAsst=null;
    let lastAsstIndex=-1;
    for(let i=messages.length-1;i>=0;i--){
      const candidate=messages[i];
      if(candidate&&candidate.role==='assistant'){
        lastAsst=candidate;
        lastAsstIndex=i;
        break;
      }
    }
    if(!lastAsst) return false;
    const projectedScene=_projectLiveAnchorActivityScene();
    const scene=_completeSettledAnchorSceneForTurn(messages,lastAsstIndex,projectedScene);
    if(scene&&Array.isArray(scene.activity_rows)&&scene.activity_rows.length){
      lastAsst._anchor_stream_id=streamId;
      lastAsst._anchor_activity_scene=scene;
      _persistSettledAnchorScene(lastAsst, scene, lastAsstIndex);
      return true;
    }
    return false;
  }
  function _upsertAnchorProcessProse(displayText, options={}){
    const text=String(displayText||'').trim();
    if(!text||!_anchorRegistry) return null;
    const segmentSeq=Number(options.segmentSeq||_anchorSegmentSeq());
    const localId=`live-prose:${streamId}:${segmentSeq}`;
    const existing=_findAnchorActivityEventByLocalId(localId,'token');
    if(existing){
      const replaced=_replaceAnchorActivityEventByLocalId(localId,'token',{
        status:options.sealed?'completed':'running',
        payload:{text,activitySegmentSeq:segmentSeq,activityBurstId:_currentActivityBurstId},
      });
      _renderAnchorLiveScene();
      return replaced;
    }
    _applyToAnchor('token',{
      text,
      local_id:localId,
      seq:_nextAnchorLocalSeq(),
      status:options.sealed?'completed':'running',
      activitySegmentSeq:segmentSeq,
      activityBurstId:_currentActivityBurstId,
    },null);
    return _findAnchorActivityEventByLocalId(localId,'token');
  }
  function _anchorHasReasoningEvents(){
    const events=_anchorActivityEvents();
    return !!(events&&events.some(event=>event&&event.source_event_type==='reasoning'));
  }
  function _upsertAnchorReasoning(text, options={}){
    const clean=String(text||'').trim();
    if(!clean||!_anchorRegistry||window._showThinking===false) return null;
    const placement=_liveThinkingPlacement();
    const segmentSeq=Number(options.segmentSeq||placement.segmentSeq||_anchorSegmentSeq());
    const localId=String(options.localId||`live-reasoning:${streamId}:${segmentSeq}`);
    const existing=_findAnchorActivityEventByLocalId(localId,'reasoning');
    if(existing){
      const replaced=_replaceAnchorActivityEventByLocalId(localId,'reasoning',{
        status:options.sealed?'completed':'running',
        payload:{text:clean,activitySegmentSeq:segmentSeq,activityBurstId:_currentActivityBurstId},
      });
      _renderAnchorLiveScene();
      return replaced;
    }
    _applyToAnchor('reasoning',{
      text:clean,
      local_id:localId,
      seq:_nextAnchorLocalSeq(),
      status:options.sealed?'completed':'running',
      activitySegmentSeq:segmentSeq,
      activityBurstId:_currentActivityBurstId,
    },null);
    return _findAnchorActivityEventByLocalId(localId,'reasoning');
  }
  function _flushReasoningToAnchor(){
    if(_anchorReasoningFlushed||!reasoningText) return;
    _anchorReasoningFlushed=true;
    if(_anchorHasReasoningEvents()) return;
    _upsertAnchorReasoning(reasoningText,{sealed:true,localId:`live-reasoning:${streamId}:final`});
  }
  function _sourceEventTypeForSnapshotAnchorRow(row){
    const source=String(row&&row.source_event_type||'').trim();
    if(source&&source!=='runtime_journal_snapshot') return source;
    const role=String(row&&row.role||'').trim();
    const kind=String(row&&row.kind||'').trim();
    if(role==='prose'||kind==='process_prose') return 'token';
    if(role==='thinking'||kind==='reasoning') return 'reasoning';
    if(role==='tool') return row&&row.status==='running'?'tool':'tool_complete';
    if(role==='terminal'||kind==='terminal_status') return row&&row.status==='running'?'compressing':'done';
    if(role==='lifecycle'||kind==='lifecycle_status') return 'compressing';
    return '';
  }
  function _hydrateAnchorRegistryFromActivityScene(scene){
    if(!_anchorRegistry||!_anchorApi||typeof _anchorApi.applyAssistantTurnAnchorSourceEvent!=='function') return false;
    if(!scene||scene.version!=='activity_scene_v1'||!Array.isArray(scene.activity_rows)||!scene.activity_rows.length) return false;
    const sceneKey=[
      scene.identity&&scene.identity.stream_id||streamId||'',
      scene.activity_rows.length,
      scene.activity_rows.map(row=>row&&row.row_id||row&&row.local_id||'').join('|'),
    ].join(':');
    if(_anchorRegistry._hydrated_activity_scene_key===sceneKey) return true;
    const rows=scene.activity_rows;
    for(let i=0;i<rows.length;i+=1){
      const row=rows[i];
      if(!row||typeof row!=='object') continue;
      const sourceType=_sourceEventTypeForSnapshotAnchorRow(row);
      if(!sourceType) continue;
      const payload={
        ...((row.payload&&typeof row.payload==='object')?row.payload:{}),
      };
      if(row.text&&!payload.text) payload.text=row.text;
      if(row.tool&&typeof row.tool==='object'){
        payload.name=payload.name||row.tool.name;
        payload.args=payload.args||row.tool.args;
        payload.preview=payload.preview||row.tool.preview;
        payload.snippet=payload.snippet||row.tool.snippet;
        payload.tid=payload.tid||row.tool.tid||row.tool.id;
        payload.id=payload.id||row.tool.id||row.tool.tid;
        payload.is_error=payload.is_error||row.tool.is_error;
        payload.duration=payload.duration||row.tool.duration;
      }
      if(row.group&&typeof row.group==='object'){
        payload.activitySegmentSeq=payload.activitySegmentSeq||row.group.activity_segment_seq;
        payload.activityBurstId=payload.activityBurstId||row.group.activity_burst_id;
      }
      const sourceEvent={
        ...payload,
        source_event_type:sourceType,
        local_id:row.local_id||row.row_id||`snapshot:${streamId}:${i}`,
        event_id:row.event_id||null,
        seq:row.seq??undefined,
        status:row.status||undefined,
        stream_id:row.stream_id||streamId,
        run_id:row.run_id||streamId,
      };
      try{
        _anchorApi.applyAssistantTurnAnchorSourceEvent(_anchorRegistry,sourceEvent,{session_id:activeSid,stream_id:streamId,run_id:streamId});
      }catch(err){
        if(!_anchorShadowWarned&&typeof console!=='undefined'&&console.warn){
          _anchorShadowWarned=true;
          console.warn('assistant turn anchor snapshot hydration failed',err);
        }
        return false;
      }
    }
    _anchorRegistry._hydrated_activity_scene_key=sceneKey;
    return true;
  }
  _hydrateAnchorRegistryFromActivityScene(INFLIGHT[activeSid]&&INFLIGHT[activeSid].anchorActivityScene);

  function _mergeSettledToolCallsWithLiveMetadata(rawCalls){
    const liveCalls=Array.isArray(S.toolCalls)?S.toolCalls:[];
    const byTid=new Map();
    liveCalls.forEach((tc,idx)=>{
      if(!tc||typeof tc!=='object') return;
      const tid=tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'';
      if(tid&&!byTid.has(tid)) byTid.set(tid,{tc,idx});
    });
    const used=new Set();
    return (rawCalls||[]).map((raw,idx)=>{
      const next={...(raw||{}),done:true};
      const tid=next.tid||next.id||next.tool_call_id||next.tool_use_id||next.call_id||'';
      let matchEntry=tid?byTid.get(tid):null;
      if(!matchEntry){
        const name=next.name||((next.function||{}).name)||'';
        const matchIdx=liveCalls.findIndex((tc,i)=>tc&&!used.has(i)&&(!name||tc.name===name));
        if(matchIdx>=0) matchEntry={tc:liveCalls[matchIdx],idx:matchIdx};
      }
      if(matchEntry){
        used.add(matchEntry.idx);
        const live=matchEntry.tc||{};
        for(const key of ['activityBurstId','duration','started_at']){
          if((next[key]===undefined||next[key]===null)&&live[key]!==undefined&&live[key]!==null) next[key]=live[key];
        }
      }
      return next;
    });
  }

  // rAF-throttled rendering: buffer tokens, render at most once per frame
  let _renderPending=false;
  // Extract display text from assistantText, stripping completed thinking blocks
  // and hiding content still inside an open thinking block.
  function _stripXmlToolCalls(s){
    // Strip <function_calls>...</function_calls> blocks (DeepSeek XML tool syntax).
    // These are processed as tool calls server-side; showing them raw in the bubble
    // looks broken. Also handles orphaned opening tags mid-stream. (#702)
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
  function _streamDisplay(){
    return _extractInlineThinkingFromContent(_stripXmlToolCalls(assistantText), liveReasoningText, {streaming:true}).content;
  }
  function _parseStreamState(){
    return _extractInlineThinkingFromContent(_stripXmlToolCalls(assistantText), liveReasoningText, {streaming:true});
  }
  function _renderLiveThinking(parsed){
    if(window._showThinking===false){removeThinking();return;}
    const text=(parsed&&parsed.thinkingText)||'';
    if(text||(parsed&&parsed.inThinking)){
      _updateLiveThinkingCard(text||'Thinking…');
      return;
    }
    // Only remove thinking if we're not in an active reasoning phase.
    // When reasoningText is set but liveReasoningText was just reset (post-tool),
    // don't wipe the finalized thinking card — it has no id anymore so
    // removeThinking() won't find it anyway, but guard explicitly.
    if(!reasoningText) removeThinking();
  }
  // Helper: create (or recreate) the smd parser bound to a given DOM element.
  // Called when assistantBody is first created and after each tool-call segment reset.
  function _smdNewParser(el, fade=false){
    _smdWrittenLen=0;
    _smdWrittenText='';
    if(!window.smd){_smdParser=null;return;}
    const baseRenderer=fade ? _streamFadeRenderer(el) : window.smd.default_renderer(el);
    const renderer=_smdRendererWithoutUnderscoreEmphasis(baseRenderer);
    _smdParser=window.smd.parser(renderer);
  }
  function _smdRendererWithoutUnderscoreEmphasis(renderer){
    if(!renderer||!window.smd) return renderer;
    const baseAddToken=renderer.add_token;
    const baseEndToken=renderer.end_token;
    const baseAddText=renderer.add_text;
    const tokenStack=[];
    renderer.add_token=(data,token)=>{
      if(token===window.smd.ITALIC_UND||token===window.smd.STRONG_UND){
        const marker=token===window.smd.STRONG_UND?'__':'_';
        tokenStack.push(marker);
        baseAddText(data,marker);
        return;
      }
      tokenStack.push(null);
      baseAddToken(data,token);
    };
    renderer.end_token=(data)=>{
      const marker=tokenStack.pop();
      if(marker){
        baseAddText(data,marker);
        return;
      }
      baseEndToken(data);
    };
    return renderer;
  }
  // Helper: end the current smd parser (flushes remaining state) and null it out.
  function _smdEndParser(){
    if(_streamingKatexTimer){clearTimeout(_streamingKatexTimer);_streamingKatexTimer=null;}
    if(_smdParser&&window.smd){
      try{window.smd.parser_end(_smdParser);}catch(_){}
      // parser_end may flush remaining markdown that creates new links/images —
      // re-sanitize the body before the DOM is handed off to highlightCode / renderMessages.
      if(assistantBody){_sanitizeSmdLinks(assistantBody);enhanceMarkdownTables(assistantBody);}
    }
    _smdParser=null;
    _smdWrittenLen=0;
    _smdWrittenText='';
  }
  function _scheduleStreamingKatex(){
    if(_streamingKatexTimer) return;
    _streamingKatexTimer=setTimeout(()=>{
      _streamingKatexTimer=null;
      if(assistantBody&&typeof renderKatexBlocks==='function') renderKatexBlocks(assistantBody,{streaming:true});
    },150);
  }
  // Helper: feed new displayText delta to the smd parser.
  // Only feeds chars beyond what has already been written (_smdWrittenLen).
  function _smdWrite(displayText, fade=false){
    if(!_smdParser||!window.smd) return;
    displayText=String(displayText||'');
    // Self-heal desyncs: if displayText no longer starts with what we've already
    // written (e.g. due to stream sanitization/tag stripping), incremental slicing
    // can skip characters. Rebuild parser from the full current displayText.
    if(_smdWrittenText && !displayText.startsWith(_smdWrittenText)){
      _smdParser=null;
      _smdWrittenLen=0;
      _smdWrittenText='';
      if(assistantBody) assistantBody.innerHTML='';
      _smdNewParser(assistantBody,fade);
      if(!_smdParser) return;
    }
    const delta=displayText.slice(_smdWrittenText.length);
    if(!delta) return;
    try{window.smd.parser_write(_smdParser,delta);}catch(_){}
    _smdWrittenLen=displayText.length;
    _smdWrittenText=displayText;
    // streaming-markdown does NOT sanitize URL schemes. The default live path
    // scans after writes; fade mode blocks unsafe href/src in its renderer.set_attr.
    if(assistantBody&&!fade){_sanitizeSmdLinks(assistantBody);}
    _scheduleStreamingKatex();
  }
  // Allowed URL schemes for anchors and images rendered from agent-streamed markdown.
  // Raw file:// anchors are rewritten to /api/media before the user can click them.
  const _SMD_SAFE_URL_RE=/^(?:https?:|mailto:|tel:|message:|\/|#|\?|\.|api|session\/)/i;
  const _SMD_SAFE_IMG_URL_RE=/^(?:https?:|mailto:|tel:|\/|#|\?|\.)/i;
  function _smdLinkHref(raw){
    const href=String(raw||'');
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
    if(!/^file:\/\//i.test(href)) return href;
    try{
      const path=decodeURIComponent(href.replace(/^file:\/\//i,''));
      return 'api/media?path='+encodeURIComponent(path)+'&inline=1';
    }catch(_){
      return 'api/media?path='+encodeURIComponent(href.replace(/^file:\/\//i,''))+'&inline=1';
    }
  }
  function _smdFileHref(raw){
    return _smdLinkHref(raw);
  }
  function _sanitizeSmdLinks(root){
    if(!root||!root.querySelectorAll) return;
    const _a=root.querySelectorAll('a[href]');
    for(let i=0;i<_a.length;i++){
      const n=_a[i],v=n.getAttribute('href')||'';
      if(/^(file|workspace|session):\/\//i.test(v)){n.setAttribute('href',_smdLinkHref(v));n.classList&&/^session:\/\//i.test(v)&&n.classList.add('session-link');continue;}
      if(!_SMD_SAFE_URL_RE.test(v)){n.removeAttribute('href');n.setAttribute('data-blocked-scheme','1');}
    }
    const _im=root.querySelectorAll('img[src]');
    for(let i=0;i<_im.length;i++){
      const n=_im[i],v=n.getAttribute('src')||'';
      if(!_SMD_SAFE_IMG_URL_RE.test(v)){n.removeAttribute('src');n.setAttribute('data-blocked-scheme','1');}
    }
  }

  function _resetStreamFadeState(){
    _streamFadeVisibleText='';
    _streamFadeLastTickMs=0;
    _streamFadeWordCarry=0;
    _streamFadeStartedAt=0;
    _streamFadeLastTargetWords=0;
    _streamFadeLastArrivalMs=0;
    _streamFadeArrivalWps=0;
    _streamFadeLatestAnimationEndAt=0;
    _streamFadeAppendOffset=0;
    _streamFadeVisibleWords=0;
    _streamFadeHoldUntilMs=0;
    _streamFadeCurrentMs=_STREAM_FADE_MS;
  }
  function _cancelAnimationFramePendingStreamRender(){
    if(_pendingRafHandle===null) return;
    cancelAnimationFrame(_pendingRafHandle);
    clearTimeout(_pendingRafHandle);
    _pendingRafHandle=null;
    _renderPending=false;
  }
  function _shouldUseStreamFade(){
    return window._fadeTextEffect===true;
  }
  function _streamFadeSkipNode(node){
    if(!node||node.nodeType!==1) return false;
    const tag=(node.tagName||'').toLowerCase();
    return tag==='pre'||tag==='code'||tag==='script'||tag==='style'||tag==='textarea'||tag==='svg'||tag==='math';
  }
  function _streamFadeReduceMotionEnabled(){
    if(!window.matchMedia) return false;
    if(!_streamFadeReduceMotionMql){
      _streamFadeReduceMotionMql=window.matchMedia('(prefers-reduced-motion: reduce)');
      _streamFadeReduceMotion=!!_streamFadeReduceMotionMql.matches;
      _streamFadeReduceMotionOnChange=e=>{_streamFadeReduceMotion=!!e.matches;};
      try{_streamFadeReduceMotionMql.addEventListener('change',_streamFadeReduceMotionOnChange);}
      catch(_){try{_streamFadeReduceMotionMql.addListener(_streamFadeReduceMotionOnChange);}catch(_){}}
    }
    return _streamFadeReduceMotion;
  }
  function _streamFadeCleanupReduceMotionListener(){
    if(!_streamFadeReduceMotionMql||!_streamFadeReduceMotionOnChange) return;
    try{_streamFadeReduceMotionMql.removeEventListener('change',_streamFadeReduceMotionOnChange);}
    catch(_){try{_streamFadeReduceMotionMql.removeListener(_streamFadeReduceMotionOnChange);}catch(_){}}
    _streamFadeReduceMotionMql=null;
    _streamFadeReduceMotionOnChange=null;
  }
  function _streamFadeBindCleanup(el){
    if(!el||el._streamFadeCleanupBound) return;
    el._streamFadeCleanupBound=true;
    el.addEventListener('animationend',e=>{
      const span=e.target;
      if(!span||!span.classList||!span.classList.contains('stream-fade-word')) return;
      span.replaceWith(document.createTextNode(span.textContent||''));
    });
  }
  function _streamFadeRenderer(el){
    _streamFadeBindCleanup(el);
    const renderer=window.smd.default_renderer(el);
    const baseAddText=renderer.add_text;
    const baseSetAttr=renderer.set_attr;
    renderer.add_text=(data,text)=>{
      const parent=data&&data.nodes&&data.nodes[data.index];
      if(!parent||_streamFadeSkipNode(parent)){baseAddText(data,text);return;}
      const frag=document.createDocumentFragment();
      const wordRe=/(\S+)(\s*)/g;
      const value=String(text||'');
      const reduceMotion=_streamFadeReduceMotionEnabled();
      const appendStartedAt=performance.now();
      let last=0, match, changed=false;
      while((match=wordRe.exec(value))){
        if(match.index>last) frag.appendChild(document.createTextNode(value.slice(last,match.index)));
        if(reduceMotion){
          frag.appendChild(document.createTextNode(match[1]));
          if(match[2]) frag.appendChild(document.createTextNode(match[2]));
          last=match.index+match[0].length;
          changed=true;
          continue;
        }
        const span=document.createElement('span');
        span.className='stream-fade-word is-new';
        const fadeMs=_streamFadeCurrentMs||_STREAM_FADE_MS;
        const delayMs=_streamFadeAppendOffset*_STREAM_FADE_STAGGER_MS;
        span.style.animationDelay=delayMs+'ms';
        if(fadeMs!==_STREAM_FADE_MS) span.style.setProperty('--stream-fade-ms',fadeMs+'ms');
        span.textContent=match[1];
        frag.appendChild(span);
        _streamFadeAppendOffset+=1;
        _streamFadeLatestAnimationEndAt=Math.max(_streamFadeLatestAnimationEndAt,appendStartedAt+delayMs+fadeMs);
        if(match[2]) frag.appendChild(document.createTextNode(match[2]));
        last=match.index+match[0].length;
        changed=true;
      }
      if(!changed){baseAddText(data,text);return;}
      if(last<value.length) frag.appendChild(document.createTextNode(value.slice(last)));
      parent.appendChild(frag);
    };
    renderer.set_attr=(data,attr,value)=>{
      const isHref=window.smd&&attr===window.smd.HREF;
      const isSrc=window.smd&&attr===window.smd.SRC;
      const safeUrl=isSrc?_SMD_SAFE_IMG_URL_RE:_SMD_SAFE_URL_RE;
      if(isHref&&/^(file|workspace|session):\/\//i.test(String(value||''))){
        baseSetAttr(data,attr,_smdLinkHref(value));
        if(/^session:\/\//i.test(String(value||''))){
          const node=data&&data.nodes&&data.nodes[data.index];
          if(node&&node.classList) node.classList.add('session-link');
        }
        return;
      }
      if((isHref||isSrc)&&!safeUrl.test(String(value||''))){
        const node=data&&data.nodes&&data.nodes[data.index];
        if(node&&node.setAttribute) node.setAttribute('data-blocked-scheme','1');
        return;
      }
      baseSetAttr(data,attr,value);
    };
    return renderer;
  }
  function _streamFadeWordCountOf(text){
    const m=String(text||'').match(/\S+/g);
    return m?m.length:0;
  }
  function _streamFadePauseAfter(text, paragraphBreakIndex){
    if(paragraphBreakIndex>=0) return 90;
    const trimmed=String(text||'').trimEnd();
    if(/[.!?]["\x27)\]]*$/.test(trimmed)) return 45;
    if(/[:;]["\x27)\]]*$/.test(trimmed)) return 30;
    return 0;
  }
  function _streamFadeNextText(targetText){
    targetText=String(targetText||'');
    const now=performance.now();
    if(!targetText){
      const hadVisible=!!_streamFadeVisibleText;
      _resetStreamFadeState();
      return {text:'', caughtUp:true, changed:hadVisible};
    }
    if(!_streamFadeVisibleText||!targetText.startsWith(_streamFadeVisibleText)){
      // Markdown/tool stripping can rewrite the visible prefix. Reset safely rather than
      // trying to animate across incompatible strings or stale word birth timestamps.
      _resetStreamFadeState();
    }
    if(!_streamFadeLastTickMs){
      _streamFadeLastTickMs=now;
      _streamFadeStartedAt=now;
    }
    if(_streamFadeVisibleText===targetText) return {text:_streamFadeVisibleText,caughtUp:true,changed:false};

    const remaining=targetText.slice(_streamFadeVisibleText.length);
    const backlogWords=_streamFadeWordCountOf(remaining);
    const targetWords=_streamFadeVisibleWords+backlogWords;
    const elapsedMs=Math.max(16,Math.min(120,now-_streamFadeLastTickMs));
    _streamFadeLastTickMs=now;

    // OpenWebUI fades the actual arriving tokens, so long/fast responses naturally
    // appear to accelerate. Hermes has a playout buffer, so track incoming word
    // velocity and play out faster than it instead of using a metronomic cadence.
    // LLM telemetry is usually tokens/sec, but the UI reveals words. A fixed word
    // cadence can look stuck even when token throughput is high, so combine:
    //   1) live target-word arrival velocity, 2) backlog pressure, 3) time ramp.
    if(!_streamFadeLastArrivalMs){
      _streamFadeLastArrivalMs=now;
      _streamFadeLastTargetWords=targetWords;
    } else if(targetWords>_streamFadeLastTargetWords){
      const arrivalElapsedMs=Math.max(16, now-_streamFadeLastArrivalMs);
      const instantArrivalWps=(targetWords-_streamFadeLastTargetWords)*1000/arrivalElapsedMs;
      // EWMA smooths bursty token chunks without hiding sustained fast output.
      _streamFadeArrivalWps=_streamFadeArrivalWps
        ? (_streamFadeArrivalWps*0.65 + instantArrivalWps*0.35)
        : instantArrivalWps;
      _streamFadeLastArrivalMs=now;
      _streamFadeLastTargetWords=targetWords;
    } else if(targetWords<_streamFadeLastTargetWords){
      _streamFadeLastTargetWords=targetWords;
      _streamFadeLastArrivalMs=now;
      _streamFadeArrivalWps=0;
    }

    if(now<_streamFadeHoldUntilMs){
      return {text:_streamFadeVisibleText,caughtUp:false,changed:false};
    }

    const streamAgeSeconds=Math.max(0, (now-(_streamFadeStartedAt||now))/1000);
    const baseWps=22 + Math.min(streamAgeSeconds*2.5, 28); // 22 → 50 wps over long answers
    const arrivalWps=_streamFadeArrivalWps ? Math.min(_streamFadeArrivalWps*1.05 + 8, 160) : 0;
    const backlogWps=backlogWords>0 ? Math.min(22 + backlogWords*1.1, 160) : 0;
    const wordsPerSecond=Math.min(160, Math.max(baseWps, arrivalWps, backlogWps));
    const speedFadeRatio=Math.max(0,Math.min(1,(wordsPerSecond-50)/(160-50)));
    _streamFadeCurrentMs=Math.round(_STREAM_FADE_MS+(_STREAM_FADE_MAX_MS-_STREAM_FADE_MS)*speedFadeRatio);

    _streamFadeWordCarry+=elapsedMs*wordsPerSecond/1000;
    if(!_streamFadeVisibleText) _streamFadeWordCarry=Math.max(_streamFadeWordCarry,1);
    let wordsToReveal=Math.floor(_streamFadeWordCarry);
    // At very high throughput, cap each frame to a small readable wave. Sustained
    // playback still catches up, but whole paragraphs no longer pop in at once.
    const waveCap=backlogWords>=160?3:2;
    wordsToReveal=Math.min(wordsToReveal,waveCap,backlogWords);
    if(wordsToReveal<1) return {text:_streamFadeVisibleText,caughtUp:false,changed:false};
    _streamFadeWordCarry=Math.max(0,_streamFadeWordCarry-wordsToReveal);

    let cut=0;
    const wordRe=/(\s*\S+\s*)/g;
    let match;
    while(wordsToReveal>0&&(match=wordRe.exec(remaining))){
      cut=wordRe.lastIndex;
      wordsToReveal-=1;
    }
    if(cut<=0) cut=Math.min(remaining.length,4);
    const chunk=remaining.slice(0,cut);
    const paragraphMatch=chunk.match(/\n\s*\n/);
    const paragraphBreak=paragraphMatch ? paragraphMatch.index : -1;
    if(paragraphMatch) cut=paragraphBreak+paragraphMatch[0].length;
    const revealed=remaining.slice(0,cut);
    _streamFadeVisibleText+=revealed;
    _streamFadeVisibleWords+=_streamFadeWordCountOf(revealed);
    const pauseMs=_streamFadePauseAfter(revealed,paragraphBreak);
    if(pauseMs) _streamFadeHoldUntilMs=now+pauseMs;
    if(_streamFadeVisibleText.length>targetText.length) _streamFadeVisibleText=targetText;
    return {text:_streamFadeVisibleText,caughtUp:_streamFadeVisibleText===targetText,changed:true};
  }
  function _renderStreamingFadeMarkdown(displayText){
    if(!assistantBody) return true;
    const next=_streamFadeNextText(displayText);
    if(!next.changed) return next.caughtUp;
    assistantBody.classList.add('stream-fade-active');
    if(!_smdParser&&window.smd){
      if(_smdReconnect){assistantBody.innerHTML='';_smdReconnect=false;}
      _smdNewParser(assistantBody,true);
    }
    if(_smdParser){
      _streamFadeAppendOffset=0;
      _smdWrite(next.text,true);
    }else{
      assistantBody.innerHTML=renderMd ? renderMd(next.text||'') : esc(next.text||'');
      _sanitizeSmdLinks(assistantBody);
    }
    return next.caughtUp;
  }
  function _streamFadeCurrentDisplayText(){
    const parsed=_parseStreamState();
    return segmentStart===0
      ? parsed.displayText
      : _stripXmlToolCalls(assistantText.slice(segmentStart));
  }
  function _drainStreamFadeBeforeDone(onDone){
    const drainStartedAt=performance.now();
    let forcedDone=false;
    const step=()=>{
      if(!assistantBody){onDone();return;}
      const target=_streamFadeCurrentDisplayText();
      const caughtUp=_renderStreamingFadeMarkdown(target);
      scrollIfPinned();
      if(caughtUp){
        // parser_end can flush pending markdown text; include that final text in
        // the fade wait instead of replacing it immediately in renderMessages().
        if(_smdParser) _smdEndParser();
        // Let the last released words visibly finish their stagger + fade before
        // the final renderMessages() DOM replacement removes the live spans.
        const remainingAnimationMs=Math.max(_STREAM_FADE_MS, _streamFadeLatestAnimationEndAt-performance.now());
        setTimeout(onDone, Math.min(remainingAnimationMs, _STREAM_FADE_DONE_MAX_MS));
        return;
      }
      // Final SSE `done` means the canonical completed session is available.
      // The optional word-fade playout must not keep that completed answer
      // hidden behind the live Thinking state for large/bursty responses.
      if(!forcedDone&&performance.now()-drainStartedAt>=_STREAM_FADE_DONE_DRAIN_MAX_MS){
        forcedDone=true;
        if(_smdParser) _smdEndParser();
        onDone();
        return;
      }
      setTimeout(()=>requestAnimationFrame(step), 33);
    };
    step();
  }
  function _flushPendingSegmentRender(options={}){
    const force=!!(options&&options.force);
    if(!assistantBody||(!force&&!_renderPending)) return;
    if(_renderPending) _cancelAnimationFramePendingStreamRender();
    const displayText=segmentStart===0
      ? _parseStreamState().displayText
      : _stripXmlToolCalls(assistantText.slice(segmentStart));
    if(_smdParser){
      _smdWrite(displayText);
    } else if(renderMd){
      assistantBody.innerHTML=renderMd(displayText);
    } else {
      assistantBody.innerHTML=esc(displayText);
    }
    _upsertAnchorProcessProse(displayText,{sealed:force});
    if(typeof _syncLiveWorklogReasonsForAnchor==='function') _syncLiveWorklogReasonsForAnchor(assistantRow, displayText);
  }
  function _resetAssistantSegment(){
    assistantRow=null;
    assistantBody=null;
    segmentStart=assistantText.length;
    _freshSegment=true;
    _smdEndParser();
    _resetStreamFadeState();
  }
  function _rememberRunJournalCursor(e){
    const raw=String(e&&e.lastEventId||'').trim();
    if(!raw) return;
    const tail=raw.includes(':')?raw.slice(raw.lastIndexOf(':')+1):raw;
    const seq=Number.parseInt(tail,10);
    if(Number.isFinite(seq)&&seq>_lastRunJournalSeq){
      _lastRunJournalSeq=seq;
      _lastRunJournalEventId=raw;
      // Mirror the advanced cursor onto the persisted INFLIGHT entry. persistInflightState()
      // saves `inflight.lastRunJournalSeq`, and a hard reload / reattach reads it back as the
      // `after_seq` replay floor (see attachLiveStream reconnecting init). Without this write
      // the persisted seq stayed 0, so a reload restored `lastAssistantText` and then replayed
      // the run journal from the zero floor (after_seq of 0) ON TOP of it — duplicating
      // already-rendered live reply content. Throttled persist keeps this off the hot token path. (#3401 reconnect dup)
      const inflight=INFLIGHT[activeSid];
      if(inflight){
        inflight.lastRunJournalSeq=seq;
        if(typeof _throttledPersist==='function') _throttledPersist();
      }
    }
  }
  function _runJournalReplayAfterSeq(){
    return Math.max(0,_lastRunJournalSeq||0);
  }
  function _runJournalReplayParams(){
    // `replay=1` documents frontend intent. The server selects replay when the
    // stream id no longer has a live worker; `after_seq` prevents duplicated
    // journal events after this EventSource has already rendered part of the
    // same run. `after_event_id` keeps that cursor run-aware so a stale cursor
    // from an earlier interrupted stream cannot suppress a newer stream whose
    // sequence numbers started over from 1.
    return `&replay=1&after_seq=${encodeURIComponent(String(_runJournalReplayAfterSeq()))}&after_event_id=${encodeURIComponent(_lastRunJournalEventId||'')}`;
  }

  function _stableStringify(value){
    const normalize=(v)=>{
      if(v===null||typeof v!=='object') return v;
      if(Array.isArray(v)) return v.map(normalize);
      const obj={};
      const keys=Object.keys(v).sort();
      for(const key of keys){
        obj[key]=normalize(v[key]);
      }
      return obj;
    };
    try{
      return JSON.stringify(normalize(value));
    }catch(_){
      return String(value||'');
    }
  }

  function _hashString(value){
    let hash=2166136261;
    for(let i=0;i<String(value||'').length;i++){
      hash^=String(value||'').charCodeAt(i);
      hash=Math.imul(hash,16777619);
    }
    return (hash>>>0).toString(16);
  }

  function _toolCallSignature(d, activityBurstId, activitySegmentSeq){
    const name=String(d&&d.name||'').trim().toLowerCase();
    const bid=Number(activityBurstId);
    const seq=Number(activitySegmentSeq);
    const args=d&&d.args;
    return `${name}|${Number.isFinite(bid)?bid:0}|${Number.isFinite(seq)?seq:0}|${_stableStringify(args)}`;
  }

  function _liveToolTid(d, activityBurstId, activitySegmentSeq){
    const explicit=String(d&&(d.tid||d.id||d.tool_call_id||d.tool_use_id||d.call_id)||'').trim();
    if(explicit) return explicit;
    return `live-${activeSid}-${_hashString(_toolCallSignature(d,activityBurstId,activitySegmentSeq))}`;
  }

  function _coerceLiveToolCallSignature(tc, activityBurstId, activitySegmentSeq){
    if(tc&&typeof tc==='object' && !tc._liveToolCallSignature){
      tc._liveToolCallSignature=_toolCallSignature(tc,activityBurstId,activitySegmentSeq);
    }
    return tc&&tc._liveToolCallSignature||'';
  }

  function _findPendingLiveToolCallIndex(toolCalls, opts){
    if(!Array.isArray(toolCalls)) return -1;
    const wantedTid=opts&&opts.tid||'';
    const wantedName=String(opts&&opts.name||'');
    const wantedSig=opts&&opts.signature||'';
    const wantedBurst=Number(opts&&opts.activityBurstId);
    const wantedSeq=Number(opts&&opts.activitySegmentSeq);
    const allowDone=!!(opts&&opts.allowDone);
    const matchName=(candidate)=>{
      return !candidate||!candidate.name||!wantedName ? false : String(candidate.name)===wantedName;
    };
    if(wantedTid){
      for(let i=toolCalls.length-1;i>=0;i--){
        const candidate=toolCalls[i];
        if(!candidate||typeof candidate!=='object') continue;
        if(!allowDone&&candidate.done===true) continue;
        const candidateTid=String(candidate.tid||candidate.id||candidate.tool_call_id||candidate.tool_use_id||candidate.call_id||'');
        if(candidateTid&&candidateTid===wantedTid) return i;
      }
    }
    if(wantedSig){
      for(let i=toolCalls.length-1;i>=0;i--){
        const candidate=toolCalls[i];
        if(!candidate||typeof candidate!=='object') continue;
        if(!allowDone&&candidate.done===true) continue;
        const canonicalSig=_coerceLiveToolCallSignature(
          candidate,
          Number.isFinite(wantedBurst)?wantedBurst:activityBurstFallbackFromCandidate(candidate),
          Number.isFinite(wantedSeq)?wantedSeq:activitySegmentSeqFallbackFromCandidate(candidate),
        );
        if(canonicalSig&&canonicalSig===wantedSig) return i;
      }
    }
    for(let i=toolCalls.length-1;i>=0;i--){
      const candidate=toolCalls[i];
      if(!candidate||typeof candidate!=='object') continue;
      if(!allowDone&&candidate.done===true) continue;
      if(!matchName(candidate)) continue;
      const candidateSeq=Number(candidate.activitySegmentSeq);
      const candidateBid=Number(candidate.activityBurstId);
      if(Number.isFinite(wantedSeq)&&Number.isFinite(candidateSeq)&&candidateSeq!==wantedSeq) continue;
      if(Number.isFinite(wantedBurst)&&Number.isFinite(candidateBid)&&candidateBid!==wantedBurst) continue;
      return i;
    }
    return -1;
  }

  function activityBurstFallbackFromCandidate(candidate){
    return Number(candidate && candidate.activityBurstId);
  }
  function activitySegmentSeqFallbackFromCandidate(candidate){
    return Number(candidate && candidate.activitySegmentSeq);
  }

  function _coerceLiveToolCallSeq(candidate){
    const raw=Number.isFinite(candidate)?candidate:Number(candidate&&candidate.activitySegmentSeq);
    return Number.isFinite(raw)&&raw>0?raw:undefined;
  }

  function _currentLiveToolAnchor(){
    const segmentSeq=Number(
      assistantRow&&assistantRow.getAttribute('data-live-segment-seq')||
      _assistantSegmentSeq||
      _currentLiveSegmentSeq||
      0
    );
    const burst=Number(_currentActivityBurstId);
    return {
      segmentSeq:Number.isFinite(segmentSeq)&&segmentSeq>0?segmentSeq:undefined,
      burstId:Number.isFinite(burst)?burst:0,
    };
  }

  function upsertLiveToolCall(d, phase){
    if(!d||d.name==='clarify') return null;
    const name=String(d&&d.name||'').trim();
    if(!name) return null;
    const current=_currentLiveToolAnchor();
    const inflight=INFLIGHT[activeSid] || (INFLIGHT[activeSid]={
      messages:[...S.messages],
      uploaded:[...uploaded],
      toolCalls:[],
    });
    if(!Array.isArray(inflight.toolCalls)) inflight.toolCalls=[];
    if(!Array.isArray(inflight.messages)) inflight.messages=[...(inflight.messages||[])];

    const explicitTid=String(d&&d.tid||d&&d.id||d&&d.tool_call_id||d&&d.tool_use_id||d&&d.call_id||'').trim();
    const isComplete=phase==='complete';
    let signature=_toolCallSignature(d,current.burstId,current.segmentSeq);
    let index=-1;

    if(explicitTid){
      index=_findPendingLiveToolCallIndex(inflight.toolCalls,{
        tid:explicitTid,
        allowDone:isComplete,
      });
    }
    if(index<0){
      index=_findPendingLiveToolCallIndex(inflight.toolCalls,{
        signature,
        name,
        activityBurstId:current.burstId,
        activitySegmentSeq:current.segmentSeq,
        allowDone:isComplete,
      });
    }
    if(index<0 && isComplete && !explicitTid){
      index=_findPendingLiveToolCallIndex(inflight.toolCalls,{
        name,
        activityBurstId:current.burstId,
        allowDone:true,
      });
    }

    let tc=null;
    if(index>=0&&inflight.toolCalls[index]){
      tc=inflight.toolCalls[index];
    }

    if(!tc){
      tc={
        name,
        preview:String(d.preview||''),
        args:d.args||{},
        snippet:'',
        done:isComplete,
        tid:explicitTid||_liveToolTid(d,current.burstId,current.segmentSeq),
        activityBurstId:current.burstId,
        activitySegmentSeq:_coerceLiveToolCallSeq(current.segmentSeq),
      };
      if(!isComplete){
        tc.started_at=Date.now()/1000;
      }
      if(isComplete) tc._createdByComplete=true;
      inflight.toolCalls.push(tc);
      if(!signature){
        signature=_toolCallSignature(tc,tc.activityBurstId,tc.activitySegmentSeq);
      }
    } else {
      if(!tc.name) tc.name=name;
      if(!tc._liveToolCallSignature){
        tc._liveToolCallSignature=_toolCallSignature(tc,tc.activityBurstId,tc.activitySegmentSeq);
      }
    }

    if(isComplete){
      if(d.preview){
        tc.snippet=tc.snippet||String(d.preview||'');
        if(!tc.preview) tc.preview=String(d.preview||'');
      }
    } else {
      tc.preview=String(d.preview||tc.preview||'');
    }
    if(d.args!==undefined) tc.args=d.args;
    if(d.snippet!==undefined) tc.snippet=d.snippet;
    tc._liveToolCallSignature = _toolCallSignature(tc,tc.activityBurstId,tc.activitySegmentSeq);
    tc.activityBurstId = Number.isFinite(Number(tc.activityBurstId))
      ? Number(tc.activityBurstId)
      : current.burstId;

    const currentSegmentSeq=_coerceLiveToolCallSeq(current.segmentSeq);
    const startSeq=_coerceLiveToolCallSeq(tc._toolCallStartSeq);
    const inferredSeq=_coerceLiveToolCallSeq(tc.activitySegmentSeq);
    if(!isComplete){
      if(inferredSeq===undefined && currentSegmentSeq!==undefined){
        tc.activitySegmentSeq=currentSegmentSeq;
      } else if(inferredSeq!==undefined){
        tc.activitySegmentSeq=inferredSeq;
      }
      tc._toolCallStartSeq=tc.activitySegmentSeq;
    } else if(startSeq!==undefined){
      tc.activitySegmentSeq=startSeq;
    } else if(inferredSeq!==undefined){
      tc.activitySegmentSeq=inferredSeq;
    }

    if(isComplete){
      tc.done=true;
      if(typeof d.is_error==='boolean') tc.is_error=d.is_error;
      if(d.duration!==undefined) tc.duration=d.duration;
      if(tc.started_at===undefined||tc.started_at===null) tc.started_at=Date.now()/1000;
      if(!tc.tid) tc.tid=explicitTid||_liveToolTid(d,tc.activityBurstId,tc.activitySegmentSeq);
    } else {
      tc.done=false;
      tc.started_at=tc.started_at||Date.now()/1000;
    }

    S.toolCalls=inflight.toolCalls;
    persistInflightState();
    return tc;
  }

  let _lastRenderMs=0;
  function _scheduleRender(){
    if(_renderPending) return;
    if(_streamFinalized) return; // Bug A: don't schedule new rAF after stream finalized
    _renderPending=true;
    // Cap render rate to ~15fps. The browser's rAF fires at 60fps, but each DOM
    // update takes 50-150ms on large sessions. During GC pauses, rAF callbacks
    // accumulate and then execute all at once, blocking the main thread for
    // multi-second stretches and crashing the renderer (Chrome error code 4/5).
    // Throttling to 66ms intervals prevents this pileup without noticeable
    // visual degradation — streaming text updates still feel immediate.
    // performance.now() is monotonic so tab suspend/resume and NTP adjustments
    // cannot produce negative or enormous deltas.
    const sinceLastMs=performance.now()-_lastRenderMs;
    const _doRender=()=>{
      _pendingRafHandle=null;
      _renderPending=false;
      // Guard: a pending setTimeout+rAF can outlive stream finalization.
      if(_streamFinalized) return;
      _lastRenderMs=performance.now();
      const parsed=_parseStreamState();
      _renderLiveThinking(parsed);
      if(assistantBody){
        const displayText = segmentStart===0
          ? parsed.displayText                          // first segment: uses think-tag stripping
          : _stripXmlToolCalls(assistantText.slice(segmentStart));
        if(_shouldUseStreamFade()){
          const caughtUp=_renderStreamingFadeMarkdown(displayText);
          if(!caughtUp&&!_streamFinalized){
            setTimeout(()=>_scheduleRender(), 33);
          }
        } else {
          assistantBody.classList.remove('stream-fade-active');
          _resetStreamFadeState();
          if(!_smdParser&&window.smd){
            // On reconnect: prior content in assistantBody came from a different smd parser run.
            // Clear it and start fresh — renderMessages() on done will restore the full content.
            if(_smdReconnect){assistantBody.innerHTML='';_smdReconnect=false;}
            _smdNewParser(assistantBody);
          }
        if(_smdParser){
          _smdWrite(displayText);
        } else {
            // Fallback: smd not loaded yet, reconnect session, or smd unavailable — use renderMd
            // for every live segment. Without this, the first segment inserts raw
            // parsed.displayText and users see unformatted markdown until done.
            const fallbackText = segmentStart===0
              ? parsed.displayText
              : _stripXmlToolCalls(assistantText.slice(segmentStart));
            assistantBody.innerHTML = renderMd ? renderMd(fallbackText) : esc(fallbackText);
          }
        }
        _upsertAnchorProcessProse(displayText);
        if(typeof _syncLiveWorklogReasonsForAnchor==='function') _syncLiveWorklogReasonsForAnchor(assistantRow, displayText);
      }
      scrollIfPinned();
      snapshotLiveTurn();
    };
    const frameIntervalMs=_shouldUseStreamFade()?33:66;
    if(sinceLastMs>=frameIntervalMs){
      _pendingRafHandle=requestAnimationFrame(_doRender);
    } else {
      _pendingRafHandle=setTimeout(()=>requestAnimationFrame(_doRender), frameIntervalMs-sinceLastMs);
    }
  }

  function _completeAutomaticCompressionOnLiveProgress(sessionId){
    const sid=String(sessionId||'');
    const hasRunningLiveCard=!!document.querySelector('[data-live-compression-card="1"][data-compression-started-at]');
    const hasRunningState=!!(window._compressionUi&&window._compressionUi.automatic&&window._compressionUi.phase==='running'&&(!sid||!window._compressionUi.sessionId||String(window._compressionUi.sessionId)===sid));
    if(!hasRunningLiveCard&&!hasRunningState) return false;
    _ensureAnchorCompressionCompletedOnLiveProgress(sid);
    if(typeof appendLiveCompressionCard==='function'){
      appendLiveCompressionCard({
        sessionId:sid,
        phase:'done',
        automatic:true,
        message:'Context auto-compressed',
      });
    }
    return true;
  }

  function _wireSSE(source){
    const existingLive=LIVE_STREAMS[activeSid];
    if(existingLive&&existingLive.source&&existingLive.source!==source){
      try{if(existingLive.source.readyState!==2)existingLive.source.close();}catch(_){ }
    }
    LIVE_STREAMS[activeSid]={streamId,source};

    // Note on #631 Bug B: the original PR description stated the server
    // "replays buffered token events" on reconnect, and proposed resetting
    // the accumulators here so the re-sent tokens wouldn't double the prefix.
    // That is NOT how the server actually works — api/routes._handle_sse_stream
    // reads a one-shot queue.Queue() that delivers each event to exactly one
    // consumer; a reconnect picks up from the current queue position and gets
    // only events produced during the outage.  Resetting the accumulators here
    // would wipe the already-displayed content and restart the response from
    // the first post-reconnect token — a real data-loss regression.
    //
    // The "doubled response" / "stuck cursor" symptom is fully explained by
    // Bug A (trailing rAF after `done` inserting a new live-turn wrapper) —
    // the fixes below (_streamFinalized guard + cancelAnimationFrame in the
    // terminal handlers) address it without needing a reset here.

    source.addEventListener('token',e=>{
      if(_terminalStateReached||_streamFinalized) return;
      const d=JSON.parse(e.data);
      assistantText+=d.text;
      syncInflightAssistantMessage();
      if(!S.session||S.session.session_id!==activeSid) return;
      _completeAutomaticCompressionOnLiveProgress(activeSid);
      const parsed=_parseStreamState();
      if(_freshSegment) appendThinking('', _liveThinkingPlacement());
      if(String((parsed&&parsed.displayText)||'').trim()||assistantRow) ensureAssistantRow();
      _scheduleRender();
    });

    source.addEventListener('interim_assistant',e=>{
      if(_terminalStateReached||_streamFinalized) return;
      const d=JSON.parse(e.data);
      const visible=String(d&&d.text?d.text:'').trim();
      const alreadyStreamed=!!(d&&d.already_streamed);
      if(!visible){
        return;
      }
      liveReasoningText='';
      if(alreadyStreamed){
        if(!S.session||S.session.session_id!==activeSid){
          recordActivityBoundary();
          _resetAssistantSegment();
          return;
        }
        _completeAutomaticCompressionOnLiveProgress(activeSid);
        const parsed=_parseStreamState();
        if(String((parsed&&parsed.displayText)||'').trim()||assistantRow){
          ensureAssistantRow(true);
          _flushPendingSegmentRender({force:true});
          if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
          if(typeof closeCurrentLiveActivityGroup==='function') closeCurrentLiveActivityGroup();
          recordActivityBoundary();
        }
        _resetAssistantSegment();
        return;
      }
      _applyToAnchor('interim_assistant',d,e);
      assistantText += assistantText ? `\n\n${visible}` : visible;
      visibleInterimSnippets.push(visible);
      syncInflightAssistantMessage();
      if(!S.session||S.session.session_id!==activeSid){
        recordActivityBoundary();
        _resetAssistantSegment();
        return;
      }
      _completeAutomaticCompressionOnLiveProgress(activeSid);
      ensureAssistantRow(true);
      if(assistantRow) assistantRow.setAttribute('data-interim','1');
      _flushPendingSegmentRender({force:true});
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      if(typeof closeCurrentLiveActivityGroup==='function') closeCurrentLiveActivityGroup();
      // Collapse old interim notes once more than INTERIM_COLLAPSE_THRESHOLD accumulate.
      const INTERIM_COLLAPSE_THRESHOLD=3;
      if(visibleInterimSnippets.length>INTERIM_COLLAPSE_THRESHOLD&&assistantRow){
        const blocks=assistantRow.parentElement;
        if(blocks){
          const allInterim=Array.from(blocks.querySelectorAll('[data-interim="1"]'));
          const toHide=allInterim.slice(0,allInterim.length-INTERIM_COLLAPSE_THRESHOLD);
          let toggle=blocks.querySelector('.interim-collapse-toggle');
          if(!toggle){
            toggle=document.createElement('span');
            toggle.className='interim-collapse-toggle';
            // No per-element listener: clicks are handled by a delegated
            // document-level handler (see _interimCollapseDelegatedClick) so
            // the toggle keeps working after a live-turn DOM restore
            // (snapshotLiveTurnHtmlForSession/restoreLiveTurnHtmlForSession
            // rebuild via innerHTML, which would drop a direct listener and
            // leave the collapsed notes permanently unreachable). The
            // threshold rides on the markup so the handler stays stateless.
            toggle.dataset.threshold=String(INTERIM_COLLAPSE_THRESHOLD);
            if(toHide.length) toHide[0].before(toggle);
          }
          // Skip re-collapse when the user expanded manually; always update the stored count.
          if(!toggle.dataset.expanded){
            toHide.forEach(el=>el.classList.add('interim-collapsed'));
          }
          const stillHidden=blocks.querySelectorAll('[data-interim="1"].interim-collapsed').length;
          if(stillHidden) toggle.textContent='Show '+stillHidden+' earlier update'+(stillHidden===1?'':'s');
        }
      }
      recordActivityBoundary();
      _resetAssistantSegment();
      _scheduleRender();
    });

    source.addEventListener('reasoning',e=>{
      if(_terminalStateReached||_streamFinalized) return;
      const d=JSON.parse(e.data);
      const text=d.text||'';
      reasoningText += text;
      liveReasoningText += text;
      if(d.text&&S.session&&S.session.session_id===activeSid) _completeAutomaticCompressionOnLiveProgress(activeSid);
      syncInflightAssistantMessage();
      if(text&&S.session&&S.session.session_id===activeSid){
        _upsertAnchorReasoning(_liveThinkingText());
        _updateLiveThinkingCard(_liveThinkingText());
      }
    });

    source.addEventListener('tool',e=>{
      if(_terminalStateReached||_streamFinalized) return;
      if(!S.session||S.session.session_id!==activeSid||S.activeStreamId!==streamId) return;
      const d=JSON.parse(e.data);
      if(d.name==='clarify') return;
      _completeAutomaticCompressionOnLiveProgress(activeSid);
      const tc=upsertLiveToolCall(d,'start');
      if(!tc) return;
      const pendingDisplayTextBeforeTool=segmentStart===0
        ? (_parseStreamState().displayText||'')
        : _stripXmlToolCalls(assistantText.slice(segmentStart));
      if(String(pendingDisplayTextBeforeTool||'').trim()) _upsertAnchorProcessProse(pendingDisplayTextBeforeTool,{sealed:true});
      _applyToAnchor('tool',{...d,...tc},e);

      if(S.session&&S.session.session_id===activeSid&&typeof scheduleRenderSessionArtifacts==='function') scheduleRenderSessionArtifacts();
      if(!S.session||S.session.session_id!==activeSid) return;
      // Provider reasoning/thinking is a Worklog Thinking Card, separate from
      // tool cards. Close the current live card before appending a tool row.
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      liveReasoningText='';
      const oldRow=$('toolRunningRow');if(oldRow)oldRow.remove();
      const pendingDisplayText=segmentStart===0
        ? (_parseStreamState().displayText||'')
        : _stripXmlToolCalls(assistantText.slice(segmentStart));
      if((assistantRow&&assistantBody)||String(pendingDisplayText||'').trim()){
        ensureAssistantRow(true);
      }
      _flushPendingSegmentRender({force:true});
      appendLiveToolCard(tc,{sessionId:activeSid,streamId});
      snapshotLiveTurn();
      _freshSegment=true;
      _smdEndParser();
      _resetAssistantSegment();
      scrollIfPinned();
    });

    source.addEventListener('tool_complete',e=>{
      if(_terminalStateReached||_streamFinalized) return;
      if(!S.session||S.session.session_id!==activeSid||S.activeStreamId!==streamId) return;
      const d=JSON.parse(e.data);
      if(d.name==='clarify') return;
      _completeAutomaticCompressionOnLiveProgress(activeSid);
      const tc=upsertLiveToolCall(d,'complete');
      if(!tc) return;
      tc.is_error=!!d.is_error;
      const pendingDisplayTextBeforeComplete=segmentStart===0
        ? (_parseStreamState().displayText||'')
        : _stripXmlToolCalls(assistantText.slice(segmentStart));
      if(String(pendingDisplayTextBeforeComplete||'').trim()) _upsertAnchorProcessProse(pendingDisplayTextBeforeComplete,{sealed:true});
      _applyToAnchor('tool_complete',{...d,...tc,is_error:!!d.is_error},e);
      if(typeof noteWorkspaceMutationsFromToolCall==='function') noteWorkspaceMutationsFromToolCall(tc);
      if(S.session&&S.session.session_id===activeSid&&typeof scheduleRenderSessionArtifacts==='function') scheduleRenderSessionArtifacts();
      if(!S.session||S.session.session_id!==activeSid) return;
      _maybeNotifyPersistentStateSaved(tc);
      if(typeof refreshOpenPreviewIfMutated==='function') refreshOpenPreviewIfMutated();
      if(tc._createdByComplete){
        const pendingDisplayText=segmentStart===0
          ? (_parseStreamState().displayText||'')
          : _stripXmlToolCalls(assistantText.slice(segmentStart));
        if((assistantRow&&assistantBody)||String(pendingDisplayText||'').trim()){
          ensureAssistantRow(true);
          _flushPendingSegmentRender({force:true});
        }
        appendLiveToolCard(tc,{sessionId:activeSid,streamId});
        _freshSegment=true;
        _smdEndParser();
        _resetAssistantSegment();
      } else {
        appendLiveToolCard(tc,{sessionId:activeSid,streamId});
      }
      snapshotLiveTurn();
      scrollIfPinned();
    });

    // Phase 2: dedicated `todo_state` event carries a full snapshot of
    // the upstream TodoStore.  We treat it as the single source of truth
    // for the Todos panel — never merge, always replace.  The handler
    // is intentionally cheap: parse, validate, write S.todos, mirror to
    // INFLIGHT, schedule a RAF render.  Out-of-order events are filtered
    // by ts; SSE journal replay is idempotent because snapshots are full.
    // Cross-session protection mirrors every other live listener:
    // payload.session_id must match activeSid or the event is dropped.
    source.addEventListener('todo_state',e=>{
      let d;
      try{ d=JSON.parse(e.data||'{}'); }catch(_){ return; }
      if(!d||typeof d!=='object') return;
      // Cross-session double check: payload.session_id is the SSE-side
      // filter (some legacy emissions omit it), and S.session.session_id
      // is the UI-side filter (a late event that arrives after the user
      // already navigated to another session must not pollute S.todos).
      // Both must agree with activeSid before we touch global state.
      if(d.session_id&&d.session_id!==activeSid) return;
      if(!S.session||S.session.session_id!==activeSid) return;
      if(!Array.isArray(d.todos)) return;
      const incomingTs=Number(d.ts)||0;
      const currentTs=(S.todoStateMeta&&Number(S.todoStateMeta.ts))||0;
      // Strictly older snapshots are discarded; equal-ts events still
      // apply so a compression-source refresh can land on the same
      // second as the tool emit it follows.
      if(incomingTs&&currentTs&&incomingTs<currentTs) return;
      S.todos=d.todos;
      S.todoStateMeta={
        ts:incomingTs||(Date.now()/1000),
        source:String(d.source||'tool'),
        version:Number(d.version)||1,
      };
      const inflight=INFLIGHT[activeSid];
      if(inflight){
        inflight.todos=S.todos;
        inflight.todoStateMeta=S.todoStateMeta;
      }
      if(typeof persistInflightState==='function') persistInflightState();
      if(typeof scheduleTodosRefresh==='function') scheduleTodosRefresh();
    });

    source.addEventListener('approval',e=>{
      const d=JSON.parse(e.data);
      _applyToAnchor('approval',d,e);
      showApprovalForSession(activeSid, d, 1);
      playAttentionSound(_attentionSoundKey(activeSid,'approval',1));
      sendBrowserNotification('Approval required',d.description||'Tool approval needed',{sid:activeSid});
    });

    source.addEventListener('clarify',e=>{
      const d=JSON.parse(e.data);
      _applyToAnchor('clarify',d,e);
      showClarifyForSession(activeSid, d);
      playAttentionSound(_attentionSoundKey(activeSid,'clarify',1));
      sendBrowserNotification('Clarification needed',d.question||'Tool clarification needed',{sid:activeSid});
    });

    source.addEventListener('state_saved',e=>{
      let d={};
      try{ d=JSON.parse(e.data||'{}'); }catch(_){}
      if((d.session_id||activeSid)!==activeSid) return;
      if(!S.session||S.session.session_id!==activeSid) return;
      _showPersistentStateToast(d.kind, d.name||'', {created:String(d.action||'').toLowerCase()==='created'});
    });

    source.addEventListener('title',e=>{
      let d={};
      try{ d=JSON.parse(e.data||'{}'); }catch(_){}
      if((d.session_id||activeSid)!==activeSid) return;
      applySessionTitleUpdate(activeSid, d.title);
    });

    source.addEventListener('title_status',e=>{
      let d={};
      try{ d=JSON.parse(e.data||'{}'); }catch(_){}
      if((d.session_id||activeSid)!==activeSid) return;
      try{
        console.info('[title]', {
          status:String(d.status||''),
          reason:String(d.reason||''),
          title:String(d.title||''),
          raw_preview:String(d.raw_preview||''),
          session_id:String(d.session_id||activeSid)
        });
      }catch(_){}
    });

    source.addEventListener('context_status',e=>{
      let d={};
      try{ d=JSON.parse(e.data||'{}'); }catch(_){}
      if((d.session_id||activeSid)!==activeSid) return;
      const prefill=d.prefill||{};
      const status=String(prefill.status||'not_configured');
      const label=String(prefill.label||'session recall');
      if(status==='loaded'){
        setComposerStatus(`Context loaded: ${label}`);
      }else if(status==='error'){
        setComposerStatus(`Context unavailable: ${label}`);
        if(typeof showToast==='function') showToast(`Context unavailable: ${String(prefill.error||label)}`,3600,'warning');
      }
    });

    function _resolveGoalMessage(d){
      const key=String(d && d.message_key ? d.message_key : '').trim();
      const args=Array.isArray(d && d.message_args) ? d.message_args : [];
      const raw=String(d&&d.message||'').trim();
      if(key && typeof t==='function'){
        try{
          const translated=String(t(key,...args));
          if(translated && translated!==key)return translated;
        }catch(_){}
      }
      return raw;
    }

    source.addEventListener('goal',e=>{
      try{
        const d=JSON.parse(e.data||'{}');
        if((d.session_id||activeSid)!==activeSid) return;
        const goalState=String(d.state||'').trim();
        const goalEvaluatingMessage=t('goal_evaluating_progress');
        if(goalState==='evaluating'){
          setComposerStatus(goalEvaluatingMessage);
          return;
        }
        const msg=_resolveGoalMessage(d);
        if(!msg)return;
        _latestGoalStatus={message:msg,decision:d.decision||null,state:goalState||null};
        setComposerStatus(msg);
        showToast(msg.split('\n')[0],2600);
      }catch(_){}
    });

    source.addEventListener('goal_continue',e=>{
      try{
        const d=JSON.parse(e.data||'{}');
        const sid=d.session_id||activeSid;
        const continuation_prompt=String(d.continuation_prompt||d.text||'').trim();
        if(!continuation_prompt||sid!==activeSid)return;
        _applyToAnchor('goal_continue',d,e);
        const _modelState=_chatPayloadModelState();
        _pendingGoalContinuation={
          sid,
          text:continuation_prompt,
          model:_modelState.model,
          model_provider:_modelState.model_provider,
          profile:S.activeProfile||'default',
        };
        const toast=t('goal_continuing_toast');
        const cmsg=_resolveGoalMessage(d);
        showToast((toast&&cmsg&&cmsg!==toast)?cmsg.split('\n')[0]:toast,2200);
      }catch(_){}
    });

    // bg_task_complete: terminal(notify_on_complete=true) background process
    // exited. Option Z PIVOT: the agent wakeup is started SERVER-SIDE by the
    // drain thread (api/background_process._process_one →
    // routes.start_session_turn) with NO browser round-trip — so the
    // closed-tab case works (parity with CLI/Telegram). The browser does NOT
    // re-POST /api/chat/start anymore. This SSE event is pure LIVE-VIEW: if
    // a tab is open the server-initiated turn streams live via the normal
    // /api/chat/stream EventSource; if the tab is closed the turn still runs
    // server-side and persists to the session store.
    //
    // Idempotency: dedupe by (session_id, event_id) via a Map+TTL ring
    // buffer (`_bgTaskCompleteRingBufferAdd`).
    //
    // Option X: this handler is the in-turn (STREAMS-bound) path. The server
    // dual-emits to the persistent session-scoped channel too — the
    // `_handleBgTaskCompleteEvent` function below is shared between both
    // paths (dedupe only; the wakeup itself is server-side).
    source.addEventListener('bg_task_complete',e=>{
      if(typeof _handleBgTaskCompleteEvent==='function'){
        _handleBgTaskCompleteEvent(e, activeSid, {source:'stream'});
      }
    });

    source.addEventListener('done',e=>{
      if(_streamFinalized) return;
      _clearStreamEndRecovery();
      if(_bailOutOfTerminalEventsFromStaleStream(source)) return;
      // Set _streamFinalized IMMEDIATELY — before any fade delay. Without this,
      // a stream_end event arriving during the fade window sees
      // _streamFinalized=false, calls _restoreSettledSession(), and overwrites
      // S.messages with stale server data (issue #3195).
      _streamFinalized=true;
      _terminalStateReached=true;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      const _doneData=JSON.parse(e.data);
      const _doneEvent=e;
      const _finishDone=()=>{
        // Bug A fix: cancel any pending rAF and mark stream finalized before
        // the DOM is settled by renderMessages, so no trailing token/reasoning rAF
        // can reintroduce a stale thinking card or duplicate content.
        _streamFinalized=true;
        _cancelAnimationFramePendingStreamRender();
        _streamFadeCleanupReduceMotionListener();
        if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
        // Finalize smd parser — flushes any remaining buffered markdown state
        // and runs Prism + copy buttons on the live segment before the DOM is replaced
        if(assistantBody){
          const _finBody=assistantBody;
          _smdEndParser();
          requestAnimationFrame(()=>{
            if(typeof highlightCode==='function') highlightCode(_finBody);
            if(typeof addCopyButtons==='function') addCopyButtons(_finBody);
            if(typeof renderKatexBlocks==='function') renderKatexBlocks();
          });
        } else {
          _smdEndParser();
        }
        const d=_doneData;
        _flushReasoningToAnchor();
        _applyToAnchor('done',{
          status:d.status||'completed',
          usage:d.usage||null,
          created_at:d.created_at||null,
        },_doneEvent);
        _scheduleAnchorRegistryCleanup();
        const isActiveSession=_isSessionCurrentPane(activeSid);
        const isSessionViewed=_isSessionActivelyViewed(activeSid);
        const completedSession=d.session||{session_id:activeSid};
        const completedSid=completedSession.session_id||activeSid;
        if(!isSessionViewed && typeof _markSessionCompletionUnread==='function'){
          _markSessionCompletionUnread(completedSid, completedSession.message_count);
        }
        _clearOwnerInflightState();
        if(typeof _markSessionCompletedInList==='function'){
          _markSessionCompletedInList(completedSession, activeSid);
        }
        _clearApprovalForOwner();
        _clearClarifyForOwner('terminal');
        const shouldFollowOnDone=isActiveSession&&((typeof _shouldFollowMessagesOnDomReplace==='function')
          ? _shouldFollowMessagesOnDomReplace()
          : (typeof _isMessagePaneNearBottom==='function'&&_isMessagePaneNearBottom(1200)));
        if(isActiveSession){
          S.activeStreamId=null;
        }
        if(isActiveSession){
          // Capture previous session totals BEFORE overwriting S.session with the new
          // cumulative values from the done event. prevIn/prevOut are the totals as of
          // the start of this turn; curIn/curOut are the full post-turn totals — the
          // delta is the per-turn usage for #1159.
          const _prevIn=(S.session&&S.session.input_tokens)||0;
          const _prevOut=(S.session&&S.session.output_tokens)||0;
          const _prevCost=(S.session&&S.session.estimated_cost)||0;
          const _prevCacheRead=(S.session&&S.session.cache_read_tokens)||0;
          const _prevCacheWrite=(S.session&&S.session.cache_write_tokens)||0;
          S.session=d.session;S.messages=_carryForwardEphemeralTurnFields(S.messages||[], d.session.messages||[]);if(typeof _messagesTruncated!=='undefined')_messagesTruncated=!!d.session._messages_truncated;
          S.messages=_filterRecoveryControlMessages(S.messages || []);
          if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
          if(typeof clearVisibleMessageRowCache==='function') clearVisibleMessageRowCache();
          if(S.session&&S.session.session_id){
            try{localStorage.setItem('hermes-webui-session',S.session.session_id);}catch(_){}
            if(typeof _setActiveSessionUrl==='function') _setActiveSessionUrl(S.session.session_id);
          }
          const _markerOnlyAssistantError=_replaceMarkerOnlyAssistantWithStreamError(S.messages);
          if(
            window._compressionUi&&window._compressionUi.automatic&&
            window._compressionUi.sessionId===activeSid&&
            d.session&&d.session.session_id
          ){
            window._compressionUi={...window._compressionUi, sessionId:d.session.session_id};
          }
          // Find the last assistant message once for both reasoning persistence and timestamp
          const lastAsst=[...S.messages].reverse().find(m=>m.role==='assistant');
          // Persist reasoning trace for Worklog Thinking Cards; normal transcript
          // rendering keeps provider reasoning out of the final answer.
          if(reasoningText&&lastAsst&&!lastAsst.reasoning) lastAsst.reasoning=reasoningText;
          // Strip any inline <think> blocks still embedded in the server-side
          // content (M3 OpenAI-compat doesn't separate reasoning). Move them
          // to m.reasoning so the persisted session stays compact and the
          // thinking card has a proper source field on reload.
          if(lastAsst && typeof lastAsst.content === 'string' && lastAsst.content){
            const split=_splitThinkFromContent(lastAsst.content, lastAsst.reasoning);
            if(split.content!==lastAsst.content){
              lastAsst.content=split.content;
              if(split.reasoning) lastAsst.reasoning=split.reasoning;
            }
          }
          // Stamp _ts on the last assistant message if it has no timestamp
          if(lastAsst&&!lastAsst._ts&&!lastAsst.timestamp) lastAsst._ts=Date.now()/1000;
          if(d.usage){
            const _doneUsageFallback={...(S.lastUsage||{})};
            if(S.session){
              for(const _usageField of ['context_length','threshold_tokens','last_prompt_tokens']){
                if(_doneUsageFallback[_usageField]==null&&S.session[_usageField]!=null){
                  _doneUsageFallback[_usageField]=S.session[_usageField];
                }
              }
            }
            S.lastUsage=typeof _mergeUsageForCtxIndicator==='function'
              ? _mergeUsageForCtxIndicator(d.usage,_doneUsageFallback)
              : {..._doneUsageFallback,...d.usage};
            _syncCtxIndicator(S.lastUsage);
            // #503 — compute per-turn cost delta and attach to last assistant message
            if(lastAsst){
              const prevIn=_prevIn;
              const prevOut=_prevOut;
              const prevCost=_prevCost;
              const curIn=d.usage.input_tokens||0;
              const curOut=d.usage.output_tokens||0;
              const curCost=d.usage.estimated_cost||0;
              const curCacheRead=d.usage.cache_read_tokens||0;
              const curCacheWrite=d.usage.cache_write_tokens||0;
              // Only set delta if values actually increased (skip no-op turns)
              if(curIn>prevIn||curOut>prevOut||curCacheRead>_prevCacheRead||curCacheWrite>_prevCacheWrite){
                lastAsst._turnUsage={
                  input_tokens:Math.max(0,curIn-prevIn),
                  output_tokens:Math.max(0,curOut-prevOut),
                  estimated_cost:Math.max(0,curCost-prevCost),
                  cache_read_tokens:Math.max(0,curCacheRead-_prevCacheRead),
                  cache_write_tokens:Math.max(0,curCacheWrite-_prevCacheWrite),
                  cache_hit_percent:d.usage.turn_cache_hit_percent,
                };
              }
              if(typeof d.usage.duration_seconds==='number'){
                lastAsst._turnDuration=d.usage.duration_seconds;
              }
              if(typeof d.usage.tps==='number'&&d.usage.tps>0){
                lastAsst._turnTps=d.usage.tps;
              }
              if(d.usage.gateway_routing){
                lastAsst._gatewayRouting=d.usage.gateway_routing;
                if(S.session)S.session.gateway_routing=d.usage.gateway_routing;
                if(S.session&&Array.isArray(S.session.gateway_routing_history))S.session.gateway_routing_history.push(d.usage.gateway_routing);
                else if(S.session)S.session.gateway_routing_history=[d.usage.gateway_routing];
              }
            }
          }
          _attachProjectedAnchorSceneToLastAssistant(S.messages);
          const hasMessageToolMetadata=S.messages.some(m=>{
            if(!m||m.role!=='assistant') return false;
            const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
            const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
            const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
            return hasTc||hasPartialTc||hasTu;
          });
          if(!hasMessageToolMetadata&&d.session.tool_calls&&d.session.tool_calls.length){
            S.toolCalls=d.session.tool_calls.map(tc=>tc);
            S.toolCalls=_mergeSettledToolCallsWithLiveMetadata(d.session.tool_calls);
          } else {
            if(hasMessageToolMetadata) S._settledLiveToolMetadata=S.toolCalls.map(tc=>({...tc,done:true}));
            S.toolCalls=hasMessageToolMetadata?[]:S.toolCalls.map(tc=>({...tc,done:true}));
          }
          if(typeof renderSessionArtifacts==='function') renderSessionArtifacts();
          if(uploaded.length){
            const lastUser=[...S.messages].reverse().find(m=>m.role==='user');
            if(lastUser)lastUser.attachments=uploaded;
          }
          if(_latestGoalStatus&&_latestGoalStatus.message){
            S.messages.push({
              role:'assistant',
              content:String(_latestGoalStatus.message),
              _ts:Date.now()/1000,
              _goalStatus:true,
              _transient:true,
            });
          }
          clearLiveToolCards();
          S.busy=false;
          // No-reply guard (#373): if agent returned nothing, show inline error
          if(!S.messages.some(m=>m.role==='assistant'&&String(m.content||'').trim())&&!assistantText){removeThinking();S.messages.push({role:'assistant',content:'**No response received.** Check your API key and model selection.'});}
          if(_markerOnlyAssistantError&&typeof showToast==='function') showToast('No response received after context compression. Please retry.',5000,'error');
          if(isSessionViewed) _markSessionViewed(completedSid, completedSession.message_count ?? S.messages.length);
          // Cooldown: prevent refreshActiveSessionIfExternallyUpdated from
          // force-reloading immediately after "done" — the event already
          // delivered the final messages and tool calls.
          if(typeof window!=='undefined') window._streamJustFinished=true;
          setTimeout(()=>{ if(typeof window!=='undefined') window._streamJustFinished=false; }, 5000);
          // Expand render window to cover all messages so the done render
          // doesn't hide Activity behind a tiny window (winSize=50).
          if(typeof _messageRenderableMessageCount==='function'&&typeof _messageRenderWindowSize!=='undefined'){
            _messageRenderWindowSize=Math.max(typeof _currentMessageRenderWindowSize==='function'?_currentMessageRenderWindowSize():50, _messageRenderableMessageCount());
          }
          syncTopbar();renderMessages({preserveScroll:true});
          if(shouldFollowOnDone&&typeof scrollToBottom==='function') scrollToBottom();
          if(typeof noteWorkspaceMutationsFromToolCalls==='function') noteWorkspaceMutationsFromToolCalls(S.toolCalls);
          loadDir('.', { preservePreview: true });
          // TTS auto-read: speak the last assistant response if enabled (#499)
          if(typeof autoReadLastAssistant==='function') setTimeout(()=>autoReadLastAssistant(), 300);
        }
        if(isActiveSession&&_pendingGoalContinuation&&typeof queueSessionMessage==='function'){
          const _goalNext=_pendingGoalContinuation;
          _pendingGoalContinuation=null;
          queueSessionMessage(_goalNext.sid,{
            text:_goalNext.text,
            files:[],
            model:_goalNext.model,
            model_provider:_goalNext.model_provider,
            profile:_goalNext.profile,
          });
          if(typeof updateQueueBadge==='function')updateQueueBadge(_goalNext.sid);
        }
        if(isActiveSession) _queueDrainSid=activeSid;
        renderSessionList();
        _setActivePaneIdleIfOwner();
        playNotificationSound();
        // #4416: notify if the tab was hidden at ANY point during this stream
        // (not just at done-receive time, which a throttled background-tab SSE
        // delivers late — after the user returns and document.hidden is false).
        // If the user watched the whole stream, _wasEverHidden stays false and
        // the notification is suppressed (matches Slack/Discord/Gmail/Claude).
        const _hiddenEntry=_STREAM_WAS_HIDDEN[activeSid];
        const _wasEverHidden=!!(_hiddenEntry&&_hiddenEntry.wasHidden);
        _clearStreamHidden(activeSid, streamId);
        sendBrowserNotification('Response complete',assistantText?assistantText.slice(0,100):'Task finished',{forceHidden:_wasEverHidden,sid:activeSid});
      };
      if(_shouldUseStreamFade()&&assistantBody){
        _cancelAnimationFramePendingStreamRender();
        _drainStreamFadeBeforeDone(_finishDone);
        return;
      }
      _finishDone();
    });

    source.addEventListener('stream_end',async e=>{
      if(_streamFinalized){
        _closeSource(source);
        return;
      }
      _clearStreamEndRecovery();
      if(_bailOutOfTerminalEventsFromStaleStream(source)) return;
      try{
        const d=JSON.parse(e.data||'{}');
        if((d.session_id||activeSid)!==activeSid) return;
      }catch(_){}
      if(S.activeStreamId===streamId && _liveStreamEndScenePresent()){
        _scheduleStreamEndRecovery(source);
        return;
      }
      // Some replay/journal paths can deliver stream_end without a preceding
      // done event. In that case closing the EventSource is not enough: the
      // live DOM/inflight state remains projected and can duplicate Thinking or
      // assistant content until a later session switch. Settle from the persisted
      // session before closing so the pane converges on canonical state.
      const status=await _restoreSettledSession(source,{status:true});
      if(status==='restored'){
        return;
      }
      if(status==='active'&&S.activeStreamId===streamId){
        _scheduleStreamEndRecovery(source,200);
        return;
      }
      _finalizeStreamEndFallback(source);
    });

    source.addEventListener('pending_steer_leftover',e=>{
      // The agent finished its turn with steer text still stashed (no
      // tool-result boundary fired). Match the CLI's leftover-delivery
      // behaviour: queue the leftover text as a next-turn user message
      // so the existing drain in setBusy(false) ships it.
      try{
        const d=JSON.parse(e.data||'{}');
        const sid=d.session_id||activeSid;
        const txt=String(d.text||'').trim();
        if(!txt||sid!==activeSid) return;
        _applyToAnchor('pending_steer_leftover',d,e);
        if(typeof queueSessionMessage==='function'){
          const _modelState=_chatPayloadModelState();
          queueSessionMessage(sid,{
            text:txt,files:[],
            model:_modelState.model,
            model_provider:_modelState.model_provider,
            profile:S.activeProfile||'default',
          });
          if(typeof updateQueueBadge==='function') updateQueueBadge(sid);
          showToast(t('steer_leftover_queued'),3000);
        }
      }catch(_){}
    });

    source.addEventListener('compressing',e=>{
      // Context auto-compression is starting. Surface the same calm running
      // compression card as manual /compress while the summarizer LLM call runs.
      if(!S.session||S.session.session_id!==activeSid) return;
      let d={};
      try{ d=JSON.parse(e.data||'{}')||{}; }catch(_){ d={}; }
      if(d.session_id&&d.session_id!==activeSid) return;
      _applyToAnchor('compressing',d,e);
      const state={
        sessionId:activeSid,
        phase:'running',
        automatic:true,
        message:'Compressing context',
        startedAt:Date.now()/1000,
      };
      if(typeof appendLiveCompressionCard==='function'&&appendLiveCompressionCard(state)){
        // Keep automatic compression inside the active Worklog. Calling
        // renderMessages() here rebuilds from the still-empty persisted
        // transcript during active streams and can erase already replayed tools.
        if(typeof clearCompressionUi==='function') clearCompressionUi();
        else window._compressionUi=null;
        snapshotLiveTurn();
        return;
      }
      if(typeof setCompressionUi==='function'){
        setCompressionUi(state);
      }
      snapshotLiveTurn();
    });

    source.addEventListener('compressed',e=>{
      // Context was auto-compressed during this turn. Keep the live timeline
      // honest by transitioning the running divider into a completed divider;
      // final settlement removes live-only compression rows from the Worklog.
      if(!S.session) return;
      const currentSid=S.session.session_id;
      let d={};
      try{ d=JSON.parse(e.data||'{}')||{}; }catch(_){ d={}; }
      const eventSid=d.old_session_id||d.session_id||activeSid;
      const continuationSid=d.new_session_id||d.continuation_session_id||'';
      const eventMatchesCurrent=!!(currentSid&&(eventSid===currentSid||d.new_session_id===currentSid||d.continuation_session_id===currentSid));
      if(!eventMatchesCurrent) return;
      _applyToAnchor('compressed',d,e);
      const displaySid=currentSid;
      if(d.usage&&typeof _syncCtxIndicator==='function'){
        S.lastUsage=typeof _mergeUsageForCtxIndicator==='function'
          ? _mergeUsageForCtxIndicator(d.usage,S.lastUsage||{})
          : {...(S.lastUsage||{}),...d.usage};
        _syncCtxIndicator(S.lastUsage);
      }
      if(typeof appendLiveCompressionCard==='function'){
        appendLiveCompressionCard({
          sessionId:displaySid,
          phase:'done',
          automatic:true,
          message:'Context auto-compressed',
          continuationSessionId:continuationSid,
        });
      }
      if(typeof clearCompressionUi==='function') clearCompressionUi();
      else window._compressionUi=null;
      if(typeof _setCompressionSessionLock==='function') _setCompressionSessionLock(null);
      if(!S.busy&&typeof renderMessages==='function') renderMessages();
    });

    source.addEventListener('metering',e=>{
      try{
        const d=JSON.parse(e.data||'{}');
        if((d.session_id||activeSid)!==activeSid) return;
        if(d.usage&&typeof _syncCtxIndicator==='function'){
          if(S.session&&S.session.session_id===activeSid){
            S.lastUsage=typeof _mergeUsageForCtxIndicator==='function'
              ? _mergeUsageForCtxIndicator(d.usage,S.lastUsage||{})
              : {...(S.lastUsage||{}),...d.usage};
            _syncCtxIndicator(S.lastUsage);
          }
        }
        if(d.estimated===true||d.tps_available!==true||typeof d.tps!=='number'||d.tps<=0){
          if(typeof _setLiveAssistantTps==='function') _setLiveAssistantTps(null);
          return;
        }
        if(typeof _setLiveAssistantTps==='function') _setLiveAssistantTps(d.tps);
      }catch(_){}
    });

    source.addEventListener('apperror',e=>{
      if(_bailOutOfTerminalEventsFromStaleStream(source)) return;
      _clearStreamEndRecovery();
      _terminalStateReached=true;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      _streamFinalized=true;
      _cancelAnimationFramePendingStreamRender();
      _streamFadeCleanupReduceMotionListener();
      _smdEndParser();
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      // Application-level error sent explicitly by the server (rate limit, crash, etc.)
      // This is distinct from the SSE network 'error' event below.
      try{if(source&&source.readyState!==2)source.close();}catch(_){ }
      _clearOwnerInflightState();
      _clearStreamHidden(activeSid, streamId);  // #4416: terminal path, drop hidden tracker
      _clearApprovalForOwner();
      _clearClarifyForOwner('terminal');
      let d={};
      try{ d=JSON.parse(e.data||'{}')||{}; }catch(_){ d={}; }
      const currentSid=S.session&&S.session.session_id;
      const eventSid=d.old_session_id||d.session_id||'';
      const continuationSid=(d.session&&d.session.session_id)||d.new_session_id||d.continuation_session_id||'';
      const eventMatchesCurrent=!!(currentSid&&(eventSid===currentSid||continuationSid===currentSid));
      if(eventMatchesCurrent){
        _flushReasoningToAnchor();
        _applyToAnchor('apperror',{
          type:d.type||'error',
          status:d.status||d.type||'error',
          message:d.message||'',
          hint:d.hint||'',
          details:d.details||'',
          session_id:d.session_id||eventSid||activeSid,
          old_session_id:d.old_session_id||null,
          new_session_id:d.new_session_id||d.continuation_session_id||null,
        },e);
      }
      if(S.session&&eventMatchesCurrent){
        S.activeStreamId=null;
        _scheduleAnchorRegistryCleanup();
        clearLiveToolCards();if(!assistantText)removeThinking();
        let isRecoveryControlMessage=false;
        try{
          const isRateLimit=d.type==='rate_limit';
          const isQuotaExhausted=d.type==='quota_exhausted';
          const isAuthMismatch=d.type==='auth_mismatch';
          const isGatewayAuthError=d.type==='gateway_auth_error';
          const isModelNotFound=d.type==='model_not_found';
          const isCancelled=d.type==='cancelled';
          const isInterrupted=d.type==='interrupted';
          const isCompressionExhausted=d.type==='compression_exhausted';
          const isToolLimitReached=d.type==='tool_limit_reached';
          isRecoveryControlMessage=isInterrupted && (d.recovery_control===true || _streamRecoveryControlMessageText(d.message));
          const isNoResponse=d.type==='no_response'||d.type==='silent_failure';
          const label=isCancelled?'Task cancelled':isInterrupted?'Response interrupted':isCompressionExhausted?'Context compression exhausted':isToolLimitReached?'Tool iteration limit reached':isQuotaExhausted?'Out of credits':isRateLimit?'Rate limit reached':isGatewayAuthError?(typeof t==='function'?t('gateway_auth_label'):'Gateway authentication failed'):isAuthMismatch?(typeof t==='function'?t('provider_mismatch_label'):'Provider mismatch'):isModelNotFound?(typeof t==='function'?t('model_not_found_label'):'Model not found'):isNoResponse?'No response from provider':'Error';
          const hint=d.hint?`\n\n*${d.hint}*`:'';
          const details=d.details?String(d.details).replace(/```/g,'`\u200b``'):'';
          const detailsLabel=isCancelled?'Cancellation details':isInterrupted?'Interruption details':isToolLimitReached?'Terminal state details':undefined;
          window._compressionUi=null;
          if(typeof clearCompressionUi==='function') clearCompressionUi();
          if(isRecoveryControlMessage){
            if(typeof showToast==='function') showToast('Stream recovery signal received. Restoring transcript...',3500,'error');
          } else if(d.session&&typeof d.session==='object'){
            S.session=d.session;
            const _nextMsgs3018=(d.session.messages||[]).filter(m=>m&&m.role);
            _attachProjectedAnchorSceneToLastAssistant(_nextMsgs3018);
            S.messages=_carryForwardEphemeralTurnFields(S.messages||[], _nextMsgs3018);
            if(S.session&&S.session.session_id){
              try{localStorage.setItem('hermes-webui-session',S.session.session_id);}catch(_){}
              if(typeof _setActiveSessionUrl==='function') _setActiveSessionUrl(S.session.session_id);
            }
          } else {
            S.messages.push({role:'assistant',content:`**${label}:** ${d.message}${hint}`,provider_details:details,provider_details_label:detailsLabel});
            _attachProjectedAnchorSceneToLastAssistant(S.messages);
          }
        }catch(_){
          S.messages.push({role:'assistant',content:'**Error:** An error occurred. Check server logs.'});
          _attachProjectedAnchorSceneToLastAssistant(S.messages);
        }
        if(isRecoveryControlMessage){
          (async()=>{
            if(await _restoreSettledSession(source)) return;
            if(S.session&&S.session.session_id===activeSid){
              S.messages=_filterRecoveryControlMessages(S.messages||[]);
              _markSessionViewed(activeSid, S.messages.length);
              renderMessages({preserveScroll:true});
            }
          })();
        } else {
          _markSessionViewed((S.session&&S.session.session_id)||activeSid, S.messages.length);
          renderMessages({preserveScroll:true});
        }
      }else if(typeof trackBackgroundError==='function'){
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        trackBackgroundError(activeSid,_errTitle,d.message||'Error');
      }
      _setActivePaneIdleIfOwner();
      renderSessionList(); // clear streaming indicator immediately on apperror
    });

    source.addEventListener('warning',e=>{
      // Non-fatal warning from server (e.g. fallback activated, retrying)
      if(!S.session||S.session.session_id!==activeSid) return;
      try{
        const d=JSON.parse(e.data);
        if(d.type==='approval_gateway_unsupported'){
          if(typeof showToast==='function') showToast(typeof t==='function'?t('approval_gateway_unsupported_label'):'Approvals not supported',4000,'warning');
          return;
        }
        // Show as a small inline notice, not a full error
        setComposerStatus(`${d.message||'Warning'}`);
        // If it's a fallback notice, show it briefly then clear
        if(d.type==='fallback') setTimeout(()=>setComposerStatus(''),4000);
      }catch(_){}
    });

    source.addEventListener('error',async e=>{
      if(_bailOutOfTerminalEventsFromStaleStream(source) && !_streamFinalized){
        return;
      }
      if(_terminalStateReached || _streamFinalized){
        _closeSource(source);
        return;
      }
      // #3885: if a stream_end recovery is in flight, don't start a competing
      // reconnect — recovery polls server state and owns the terminal decision
      // (else its exhaustion could mute a freshly reconnected stream). Opus stage-LK.
      if(_pendingStreamEndRecovery){
        _closeSource(source);
        return;
      }
      if(typeof recordClientSSEError==='function') recordClientSSEError('chat-response',{ready_state:source?source.readyState:null,session_id:activeSid,stream_id:streamId,reason:'chat EventSource.onerror'});
      try{if(source&&source.readyState!==2)source.close();}catch(_){ }
      if(_deferStreamErrorIfOffline()) return;
      if(_deferStreamErrorIfPageHidden(source)) return;
      _closeSource(source);
      // If the user has switched to a different session, don't attempt to
      // reconnect — the old stream's EventSource was closed intentionally
      // during session switch and reconnecting would leak a background stream.
      if(!_isSessionCurrentPane(activeSid)) return;
      if(_terminalStateReached || _streamFinalized){
        return;
      }
      // Attempt one reconnect if the stream is still active server-side
      if(!_reconnectAttempted && streamId){
        _reconnectAttempted=true;
        setComposerStatus('Reconnecting…');
        setTimeout(async()=>{
          try{
            const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
            if(st.active){
              setComposerStatus('Reconnected');
              _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,document.baseURI||location.href).href,{withCredentials:true}));
              return;
            }
            if(st.replay_available){
              setComposerStatus('Restoring stream…');
              _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}${_runJournalReplayParams()}`,document.baseURI||location.href).href,{withCredentials:true}));
              return;
            }
          }catch(_){
            if(_deferStreamErrorIfOffline()) return;
          }
          if(await _restoreSettledSession(source)) return;
          if(_deferStreamErrorIfOffline()) return;
          if(_deferStreamErrorIfPageHidden(source)) return;
          _flushReasoningToAnchor();
          _scheduleAnchorRegistryCleanup(120000);
          _handleStreamError(source);
        },1500);
        return;
      }
      if(await _restoreSettledSession(source)) return;
      if(_deferStreamErrorIfOffline()) return;
      if(_deferStreamErrorIfPageHidden(source)) return;
      _flushReasoningToAnchor();
      _scheduleAnchorRegistryCleanup(120000);
      _handleStreamError(source);
    });

    source.addEventListener('cancel',e=>{
      if(_bailOutOfTerminalEventsFromStaleStream(source)) return;
      _clearStreamEndRecovery();
      _terminalStateReached=true;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      _streamFinalized=true;
      _cancelAnimationFramePendingStreamRender();
      _streamFadeCleanupReduceMotionListener();
      _smdEndParser();
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      try{if(source&&source.readyState!==2)source.close();}catch(_){ }
      _clearOwnerInflightState();
      _clearStreamHidden(activeSid, streamId);  // #4416: terminal path, drop hidden tracker
      _clearApprovalForOwner();
      _clearClarifyForOwner('cancelled');
      let _cancelData={};
      try{ _cancelData=JSON.parse(e.data||'{}')||{}; }catch(_){ _cancelData={}; }
      _flushReasoningToAnchor();
      _applyToAnchor('cancel',{
        status:_cancelData.status||_cancelData.type||'cancelled',
        message:_cancelData.message||'',
        session_id:_cancelData.session_id||activeSid,
      },e);
      _scheduleAnchorRegistryCleanup();
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;
      }
      // Fetch latest session from server to get accurate message list (includes cancel status)
      // This ensures messages stay in sync with server, fixing race condition where local
      // "*Task cancelled.*" message gets lost when done event overwrites S.messages
      (async()=>{
        try{
          const data=await api(`/api/session?session_id=${encodeURIComponent(activeSid)}`);
          if(data&&data.session&&S.session&&S.session.session_id===activeSid){
            S.session=data.session;
            const _nextMsgs3018=(data.session.messages||[]).filter(m=>m&&m.role);
            _attachProjectedAnchorSceneToLastAssistant(_nextMsgs3018);
            S.messages=_carryForwardEphemeralTurnFields(S.messages||[], _nextMsgs3018);
            if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
            clearLiveToolCards();if(!assistantText)removeThinking();
            _markSessionViewed(activeSid, data.session.message_count ?? S.messages.length);
            renderMessages({preserveScroll:true});
          }
        }catch(_){
          // Fallback to local cancel message if API fails
          if(S.session&&S.session.session_id===activeSid){
            clearLiveToolCards();if(!assistantText)removeThinking();
            const cancelAgentName=(assistantDisplayName()+'').trim()||'Hermes';
            S.messages.push({role:'assistant',content:`**Task cancelled:** Task cancelled.\n\n*The run was cancelled by the user before ${cancelAgentName} finished. No provider failure occurred.*`,provider_details:'Task cancelled.',provider_details_label:'Cancellation details',_error:true});
            _attachProjectedAnchorSceneToLastAssistant(S.messages);
            renderMessages({preserveScroll:true});
            _markSessionViewed(activeSid, S.messages.length);
          }
        }
      })();
      renderSessionList();
      _setActivePaneIdleIfOwner();
    });

    for(const _runJournalEventName of ['token','interim_assistant','reasoning','tool','tool_complete','todo_state','approval','clarify','state_saved','title','title_status','context_status','goal','goal_continue','done','stream_end','pending_steer_leftover','compressing','compressed','metering','apperror','warning','error','cancel']){
      source.addEventListener(_runJournalEventName,_rememberRunJournalCursor);
    }
  }

  // #3018: per-turn ephemeral fields are computed client-side in _finishDone
  // and attached to message objects (S.messages). When a server refresh
  // (loadSession, _restoreSettledSession, external active-session poll,
  // SSE error recovery) replaces S.messages with fresh server data, those
  // fields are dropped and the usage badge / duration / gateway routing
  // pill flashes-then-disappears. Carry them forward by matching messages
  // on (role, timestamp, content prefix) — the same identity the renderer
  // already uses for stable keys.
  function _messageIdentityKey(m){
    if(!m||!m.role) return '';
    const ts=m._ts||m.timestamp||'';
    let body='';
    if(typeof m.content==='string') body=m.content;
    else if(Array.isArray(m.content)){
      try{ body=m.content.map(p=>(p&&typeof p==='object')?(p.text||p.input_text||'')||'':String(p||'')).join('').slice(0,160); }catch(_){ body=''; }
    }
    return `${m.role}|${ts}|${body.slice(0,160)}`;
  }
  const _EPHEMERAL_TURN_FIELDS=['_turnUsage','_turnDuration','_turnTps','_gatewayRouting','_statusCard','_anchor_stream_id','_anchor_activity_scene'];
  function _carryForwardEphemeralTurnFields(prevMessages, nextMessages){
    if(!Array.isArray(prevMessages)||!Array.isArray(nextMessages)) return nextMessages;
    if(!prevMessages.length||!nextMessages.length) return nextMessages;
    const prevIdx=new Map();
    for(const pm of prevMessages){
      const k=_messageIdentityKey(pm); if(!k) continue;
      // If duplicate keys, prefer the latest occurrence (it carries the
      // most-recently-attached ephemeral state).
      prevIdx.set(k,pm);
    }
    for(const nm of nextMessages){
      const k=_messageIdentityKey(nm); if(!k) continue;
      const pm=prevIdx.get(k); if(!pm) continue;
      for(const f of _EPHEMERAL_TURN_FIELDS){
        if(pm[f]!=null && nm[f]==null) nm[f]=pm[f];
      }
    }
    return nextMessages;
  }
  if(typeof window!=='undefined'){
    window._carryForwardEphemeralTurnFields=_carryForwardEphemeralTurnFields;
  }

  async function _restoreSettledSession(source, options=null){
    const returnStatus=!!(options&&options.status);
    if(_isActiveSession() && S.activeStreamId!==streamId){
      _closeSource(source);
      return returnStatus?'stale':false;
    }
    try{
      const data=await api(`/api/session?session_id=${encodeURIComponent(activeSid)}`);
      // Opus #2852 race-fix: if a late `done` event ran the finalize path while
      // we were awaiting the network roundtrip, bail out — done already settled.
      if(_streamFinalized) return returnStatus?'restored':true;
      const session=data&&data.session;
      if(!session) return returnStatus?'missing':false;
      if(session.active_stream_id||session.pending_user_message) return returnStatus?'active':false;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      _streamFinalized=true;
      _cancelAnimationFramePendingStreamRender();
      _streamFadeCleanupReduceMotionListener();
      _smdEndParser();
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      _clearOwnerInflightState();
      _flushReasoningToAnchor();
      _scheduleAnchorRegistryCleanup();
      _closeSource(source);
      _clearApprovalForOwner();
      _clearClarifyForOwner('terminal');
      const isSessionViewed=_isSessionActivelyViewed(activeSid);
      const completedSid=session.session_id||activeSid;
      if(!isSessionViewed && typeof _markSessionCompletionUnread==='function'){
        _markSessionCompletionUnread(completedSid, session.message_count);
      }
      const isActiveSession=_isSessionCurrentPane(activeSid);
      if(isActiveSession){
        S.activeStreamId=null;
        clearLiveToolCards();if(!assistantText)removeThinking();
        S.session=session;
        const _nextMsgs3018=(session.messages||[]).filter(m=>m&&m.role);
        _attachProjectedAnchorSceneToLastAssistant(_nextMsgs3018);
        S.messages=_carryForwardEphemeralTurnFields(S.messages||[], _nextMsgs3018);
        S.messages=_filterRecoveryControlMessages(S.messages || []);
        if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
        if(S.session&&S.session.session_id){
          try{localStorage.setItem('hermes-webui-session',S.session.session_id);}catch(_){}
          if(typeof _setActiveSessionUrl==='function') _setActiveSessionUrl(S.session.session_id);
        }
        const _markerOnlyAssistantError=_replaceMarkerOnlyAssistantWithStreamError(S.messages);
        if(_markerOnlyAssistantError&&typeof showToast==='function') showToast('No response received after context compression. Please retry.',5000,'error');
        const hasMessageToolMetadata=S.messages.some(m=>{
          if(!m||m.role!=='assistant') return false;
          // Recognize both the standard `tool_calls` (used by completed assistant
          // turns where the LLM emitted tool_call entries) and the WebUI-internal
          // `_partial_tool_calls` (used on Stop/Cancel partial messages — see
          // api/streaming.py cancel_stream).
          const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
          const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
          const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
          return hasTc||hasPartialTc||hasTu;
        });
        if(!hasMessageToolMetadata&&session.tool_calls&&session.tool_calls.length){
          S.toolCalls=_mergeSettledToolCallsWithLiveMetadata(session.tool_calls||[]);
        }else{
          if(hasMessageToolMetadata) S._settledLiveToolMetadata=S.toolCalls.map(tc=>({...tc,done:true}));
          S.toolCalls=[];
        }
        if(isSessionViewed) _markSessionViewed(completedSid, session.message_count ?? S.messages.length);
        // Expand render window so the settled render doesn't hide Activity.
        if(typeof _messageRenderableMessageCount==='function'&&typeof _messageRenderWindowSize!=='undefined'){
          _messageRenderWindowSize=Math.max(typeof _currentMessageRenderWindowSize==='function'?_currentMessageRenderWindowSize():50, _messageRenderableMessageCount());
        }
        syncTopbar();renderMessages({preserveScroll:true});
      }
      if(_isActiveSession()) _queueDrainSid=activeSid;
      renderSessionList();
      _setActivePaneIdleIfOwner();
      return returnStatus?'restored':true;
    }catch(_){
      return returnStatus?'error':false;
    }
  }

  function _handleStreamError(source){
    if(_isActiveSession() && S.activeStreamId!==streamId){
      _closeSource(source);
      return;
    }
    _clearStreamEndRecovery();
    // Opus review Q1: mirror done/apperror/cancel finalization so any pending rAF
    // cannot fire after renderMessages() has settled the DOM with the error message.
    if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
    _streamFinalized=true;
    _cancelAnimationFramePendingStreamRender();
    _streamFadeCleanupReduceMotionListener();
    if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
    _clearOwnerInflightState();
    _closeSource(source);
    _clearApprovalForOwner();
    _clearClarifyForOwner('terminal');
    if(S.session&&S.session.session_id===activeSid){
      S.activeStreamId=null;
      _flushReasoningToAnchor();
      _applyToAnchor('error',{
        status:'connection_lost',
        message:'The browser lost the live SSE connection before the response finished.',
        session_id:activeSid,
      },null);
      clearLiveToolCards();if(!assistantText)removeThinking();
      S.messages.push({role:'assistant',content:'**Connection interrupted:** The browser lost the live SSE connection before the response finished. If the worker completed, reopening this session should restore the settled transcript.'});
      _attachProjectedAnchorSceneToLastAssistant(S.messages);
      renderMessages({preserveScroll:true});
      _markSessionViewed(activeSid, S.messages.length);
    }else{
      if(typeof trackBackgroundError==='function'){
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        trackBackgroundError(activeSid,_errTitle,'Connection interrupted');
      }
    }
    _setActivePaneIdleIfOwner();
  }

  (async()=>{
    // Reattach path can carry stale stream ids after server restart; preflight
    // status avoids opening a dead SSE URL that will 404 in the console.
    let replayOnly=false;
    if(reconnecting){
      try{
        const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
        if(!st.active&&st.replay_available){
          replayOnly=true;
        }else if(!st.active){
          _clearOwnerInflightState();
          _clearApprovalForOwner();
          _clearClarifyForOwner('terminal');
          if(S.session&&S.session.session_id===activeSid){
            S.activeStreamId=null;
            clearLiveToolCards();
            removeThinking();
            if(_isActiveSession()) _queueDrainSid=activeSid;
            _setActivePaneIdleIfOwner();
            renderMessages({preserveScroll:true});
            renderSessionList();
          }
          _scheduleAnchorRegistryCleanup(120000);
          return;
        }
      }catch(_){}
    }
    const replayParams=(reconnecting||replayOnly)?_runJournalReplayParams():'';
    _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}${replayParams}`,document.baseURI||location.href).href,{withCredentials:true}));
  })();

}

function transcript(){
  const lines=[`# Hermes session ${S.session?.session_id||''}`,``,
    `Workspace: ${S.session?.workspace||''}`,`Model: ${S.session?.model||''}`,``];
  for(const m of S.messages){
    if(!m||m.role==='tool')continue;
    let c=m.content||'';
    if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('\n');
    const ct=String(c).trim();
    if(!ct&&!m.attachments?.length)continue;
    const attach=m.attachments?.length?`\n\n_Files: ${m.attachments.join(', ')}_`:'';
    lines.push(`## ${m.role}`,'',ct+attach,'');
  }
  return lines.join('\n');
}

let _composerAutoResizeRaf=0;
function autoResize(){
  if(_composerAutoResizeRaf && typeof cancelAnimationFrame==='function'){
    cancelAnimationFrame(_composerAutoResizeRaf);
    _composerAutoResizeRaf=0;
  }
  const el=$('msg');
  el.style.height='auto';
  el.style.height=Math.min(el.scrollHeight,200)+'px';
  updateSendBtn();
}
function scheduleComposerAutoResize(){
  if(typeof requestAnimationFrame!=='function'){autoResize();return;}
  if(_composerAutoResizeRaf) return;
  _composerAutoResizeRaf=requestAnimationFrame(()=>{
    _composerAutoResizeRaf=0;
    autoResize();
  });
}


// ── YOLO mode state ──
// Session-scoped; stored server-side in memory (tools/approval.py).
// Lifecycle:
//   • Page reload: state PERSISTS — _fetchYoloState() re-syncs from backend.
//   • Cross-tab: state is SHARED — enabling YOLO in Tab A affects Tab B for
//     the same session (both poll the same server-side flag).
//   • Server restart: state is LOST — in-memory only, not persisted to disk.
//   • Session switch: state resets — loadSession() clears _yoloEnabled and
//     fetches the new session's state.
let _yoloEnabled = false;

async function _fetchYoloState(sid) {
  try {
    const data = await api('/api/session/yolo?session_id=' + encodeURIComponent(sid));
    _yoloEnabled = !!data.yolo_enabled;
    _updateYoloPill();
  } catch (_) { /* ignore */ }
}

function _updateYoloPill() {
  const pill = $('yoloPill');
  if (!pill) return;
  pill.style.display = _yoloEnabled ? '' : 'none';
  if (_yoloEnabled) {
    pill.title = t('yolo_pill_title_active');
    pill.setAttribute('data-i18n-title', 'yolo_pill_title_active');
  }
  if (typeof applyLocaleToDOM === 'function') applyLocaleToDOM();
}

async function toggleYoloFromApproval() {
  const sid = S.session && S.session.session_id;
  if (!sid) return;
  try {
    await api('/api/session/yolo', {
      method: 'POST',
      body: JSON.stringify({ session_id: sid, enabled: true }),
    });
    _yoloEnabled = true;
    _updateYoloPill();
    hideApprovalCard(true);
    showToast(t('yolo_enabled'));
  } catch (e) { showToast('YOLO: ' + e.message); }
}

// ── Approval polling ──
let _approvalPollTimer = null;
let _approvalFallbackPollInFlight = false;
let _approvalHideTimer = null;
let _approvalVisibleSince = 0;
let _approvalSignature = '';
const APPROVAL_MIN_VISIBLE_MS = 30000;

// showApprovalCard moved above respondApproval

function _clearApprovalHideTimer() {
  if (_approvalHideTimer) {
    clearTimeout(_approvalHideTimer);
    _approvalHideTimer = null;
  }
}

function _resetApprovalCardState() {
  _clearApprovalHideTimer();
  _approvalVisibleSince = 0;
  _approvalSignature = '';
}

function hideApprovalCard(force=false) {
  const card = $("approvalCard");
  if (!card) return;
  if (!force && _approvalVisibleSince) {
    const remaining = APPROVAL_MIN_VISIBLE_MS - (Date.now() - _approvalVisibleSince);
    if (remaining > 0) {
      const scheduledSignature = _approvalSignature;
      _clearApprovalHideTimer();
      _approvalHideTimer = setTimeout(() => {
        _approvalHideTimer = null;
        if (_approvalSignature !== scheduledSignature) return;
        hideApprovalCard(true);
      }, remaining);
      return;
    }
  }
  _approvalSessionId = null;
  _resetApprovalCardState();
  card.classList.remove("visible");
  card.classList.remove("collapsed");
  _syncApprovalTranscriptSpace(null);
  $("approvalCmd").textContent = "";
  $("approvalDesc").textContent = "";
}

// Track session_id of the active approval so respond goes to the right session
let _approvalSessionId = null;
let _approvalCurrentId = null;  // approval_id of the card currently shown
let _approvalPendingBySession = new Map();

function _promptActiveSessionId() {
  return (S.session && S.session.session_id) || null;
}

function _approvalPromptBelongsToActiveSession(sid) {
  return !!(sid && _promptActiveSessionId() === sid);
}

function activeSessionHasPendingPromptAttention() {
  const sid = _promptActiveSessionId();
  return !!(sid && (
    _approvalPendingBySession.has(sid) ||
    _clarifyPendingBySession.has(sid)
  ));
}

function _rememberApprovalPending(pending, pendingCount) {
  if (!pending) return null;
  const sid = pending._session_id || _promptActiveSessionId();
  if (!sid) return null;
  const nextPending = {...pending, _session_id: sid};
  _approvalPendingBySession.set(sid, {pending: nextPending, pendingCount: pendingCount || 1});
  return sid;
}

function _clearApprovalPendingForSession(sid) {
  if (sid) {
    _approvalPendingBySession.delete(sid);
    if (typeof syncTopbar === 'function') syncTopbar();
  }
}

function _hideApprovalCardIfOwner(sid, force=false) {
  if (!sid || _approvalSessionId === sid) hideApprovalCard(force);
}

function _approvalPollingSessionMissingOrMismatched(sid) {
  return !sid || !S.session || S.session.session_id !== sid;
}

function _renderPendingApprovalForActiveSession() {
  const sid = _promptActiveSessionId();
  if (!sid) return;
  if (_approvalSessionId && _approvalSessionId !== sid) hideApprovalCard(true);
  const entry = _approvalPendingBySession.get(sid);
  if (entry) showApprovalCard(entry.pending, entry.pendingCount);
}

function showApprovalForSession(sid, pending, pendingCount) {
  if (!pending) return;
  pending._session_id = sid;
  showApprovalCard(pending, pendingCount);
}

function showApprovalCard(pending, pendingCount) {
  const sid = _rememberApprovalPending(pending, pendingCount);
  if (!_approvalPromptBelongsToActiveSession(sid)) return;
  const keys = pending.pattern_keys || (pending.pattern_key ? [pending.pattern_key] : []);
  const desc = (pending.description || "") + (keys.length ? " [" + keys.join(", ") + "]" : "");
  const cmd = pending.command || "";
  const sig = JSON.stringify({desc, cmd, sid: pending._session_id || (S.session && S.session.session_id) || null, approval_id: pending.approval_id || null});
  const card = $("approvalCard");
  const sameApproval = card.classList.contains("visible") && _approvalSignature === sig;
  $("approvalDesc").textContent = desc;
  $("approvalCmd").textContent = cmd;
  _approvalSessionId = sid;
  _approvalCurrentId = pending.approval_id || null;
  _approvalSignature = sig;
  // Show "1 of N" counter when multiple approvals are queued
  const counter = $("approvalCounter");
  if (counter) {
    if (pendingCount && pendingCount > 1) {
      counter.textContent = "1 of " + pendingCount + " pending";
      counter.style.display = "";
    } else {
      counter.style.display = "none";
    }
  }
  if (!sameApproval) {
    _approvalVisibleSince = Date.now();
    _clearApprovalHideTimer();
    // A distinct approval must always render expanded — never inherit a prior
    // approval's collapsed state, which would hide its command + action buttons. (#3515)
    card.classList.remove("collapsed");
  }
  // Re-enable buttons in case a previous approval disabled them
  ["approvalBtnOnce","approvalBtnSession","approvalBtnAlways","approvalBtnDeny"].forEach(id => {
    const b = $(id); if (b) { b.disabled = false; b.classList.remove("loading"); }
  });
  card.classList.add("visible");
  _syncApprovalCollapseButton(card);
  _syncApprovalTranscriptSpace(card, {immediate: true});
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  const onceBtn = $("approvalBtnOnce");
  if (onceBtn && document.activeElement !== $('msg')) {
    setTimeout(() => onceBtn.focus({preventScroll: true}), 50);
  }
  if (typeof syncTopbar === 'function') syncTopbar();
}

function _syncApprovalCollapseButton(card) {
  const collapse = $("approvalCollapse");
  if (!collapse || !card) return;
  const collapsed = card.classList.contains("collapsed");
  collapse.setAttribute("aria-expanded", collapsed ? "false" : "true");
  // Icon swap: chevron-down when expanded (click to collapse), chevron-up when collapsed (click to expand)
  const polyline = collapse.querySelector("svg polyline");
  if (polyline) polyline.setAttribute("points", collapsed ? "18 15 12 9 6 15" : "6 9 12 15 18 9");
  const label = collapsed ? "Expand approval" : "Collapse approval";
  collapse.setAttribute("aria-label", label);
  collapse.title = label;
}

function _approvalMessagesNearBottom(messages) {
  if (!messages) return false;
  return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 150;
}

function _syncApprovalTranscriptSpace(card, opts) {
  opts = opts || {};
  const messages = $("messages");
  if (!messages) return;
  const wasNearBottom = _approvalMessagesNearBottom(messages);
  if (!card || !card.classList.contains("visible")) {
    messages.classList.remove("approval-open");
    messages.classList.remove("approval-collapsed");
    messages.style.removeProperty("--approval-card-height");
    messages.style.removeProperty("--approval-dock-height");
    if (wasNearBottom && typeof scrollToBottom === "function" && typeof requestAnimationFrame === "function") {
      requestAnimationFrame(scrollToBottom);
    }
    return;
  }
  const collapsed = card.classList.contains("collapsed");
  messages.classList.add("approval-open");
  messages.classList.toggle("approval-collapsed", collapsed);
  const measure = () => {
    if (!card.classList.contains("visible")) return;
    const target = collapsed ? card : (card.querySelector(".approval-inner") || card);
    const h = target && target.getBoundingClientRect().height;
    if (h > 0) {
      messages.style.setProperty(collapsed ? "--approval-dock-height" : "--approval-card-height", Math.ceil(h + 24) + "px");
    }
    if (wasNearBottom && typeof scrollToBottom === "function") scrollToBottom();
  };
  if (opts.immediate) measure();
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(measure);
  setTimeout(measure, 420);
}

function toggleApprovalCardCollapsed(forceCollapsed) {
  const card = $("approvalCard");
  if (!card) return;
  const collapsed = typeof forceCollapsed === "boolean" ? forceCollapsed : !card.classList.contains("collapsed");
  card.classList.toggle("collapsed", collapsed);
  _syncApprovalCollapseButton(card);
  _syncApprovalTranscriptSpace(card, {immediate: true});
}

async function respondApproval(choice) {
  const sid = _approvalSessionId || (S.session && S.session.session_id);
  if (!sid) return;
  const approvalId = _approvalCurrentId;
  // Disable all buttons immediately to prevent double-submit
  ["approvalBtnOnce","approvalBtnSession","approvalBtnAlways","approvalBtnDeny"].forEach(id => {
    const b = $(id);
    if (b) { b.disabled = true; if (b.id === "approvalBtn" + choice.charAt(0).toUpperCase() + choice.slice(1)) b.classList.add("loading"); }
  });
  _approvalSessionId = null;
  _approvalCurrentId = null;
  _clearApprovalPendingForSession(sid);
  hideApprovalCard(true);
  try {
    await api("/api/approval/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: sid, choice, approval_id: approvalId })
    });
  } catch(e) { setStatus(t("approval_responding") + " " + e.message); }
}

function startApprovalPolling(sid) {
  stopApprovalPolling();
  _approvalPollingSessionId = sid || null;

  // Use HTTP polling instead of SSE to avoid browser connection pool exhaustion.
  // Browsers limit to 6 concurrent HTTP connections per origin over HTTP/1.1.
  // With 6 persistent SSE streams (sessions/events, gateway/stream,
  // session/stream, approval/stream, clarify/stream, chat/stream), the pool
  // fills and all fetch() requests queue indefinitely. The server responds
  // normally (curl works), but the browser has no available sockets.
  //
  // This was introduced in v0.51.340 when /api/session/stream was added as
  // the 6th persistent SSE connection. Until we multiplex streams or serve
  // SSE from a separate origin, use HTTP polling to free 2 connection slots.
  // (1.5-second interval, acceptable tradeoff)
  _startApprovalFallbackPoll(sid);
}

let _approvalEventSource = null;
let _approvalSSEHealthTimer = null;
let _approvalPollingSessionId = null;

function _startApprovalFallbackPoll(sid) {
  // Run one tick immediately so a session already blocked on a pending approval
  // shows its card instantly (the removed SSE 'initial' event used to do this);
  // then poll on the 1500ms cadence. (#3913 SHOULD-FIX)
  const _tick = async () => {
    if (_approvalPollingSessionMissingOrMismatched(sid)) {
      stopApprovalPolling(); _hideApprovalCardIfOwner(sid, true); return;
    }
    if (_approvalFallbackPollInFlight) return;
    _approvalFallbackPollInFlight = true;
    try {
      const data = await api("/api/approval/pending?session_id=" + encodeURIComponent(sid),{timeoutToast:false});
      if (data.pending) { showApprovalForSession(sid, data.pending, data.pending_count||1); }
      else if (!_approvalPollingSessionMissingOrMismatched(sid)) {
        _clearApprovalPendingForSession(sid);
        _hideApprovalCardIfOwner(sid);
        if (!S.busy) {
          stopApprovalPollingForSession(sid);
        }
      }
    } catch(e) { /* ignore poll errors */ }
    finally { _approvalFallbackPollInFlight = false; }
  };
  _approvalPollTimer = setInterval(_tick, 1500);  // matches the v0.50.247 polling cadence so degraded-mode users see the same responsiveness
  _tick();
}

function stopApprovalPollingForSession(sid) {
  if(sid && _approvalPollingSessionId && _approvalPollingSessionId!==sid) return;
  stopApprovalPolling();
}

function stopApprovalPolling() {
  if (_approvalPollTimer) { clearInterval(_approvalPollTimer); _approvalPollTimer = null; }
  if (_approvalEventSource) { try { if(_approvalEventSource.readyState!==2)_approvalEventSource.close(); } catch(_){} _approvalEventSource = null; }
  if (_approvalSSEHealthTimer) { clearInterval(_approvalSSEHealthTimer); _approvalSSEHealthTimer = null; }
  _approvalFallbackPollInFlight = false;
  _approvalPollingSessionId = null;
}

// ── Session-scoped SSE stream (Option X) ──────────────────────────────────
// Long-lived EventSource bound to /api/session/stream?session_id=<sid>.
// Lives across agent turns (unlike the per-turn /api/chat/stream which is
// torn down at end-of-turn). Carries bg_task_complete events fired while no
// turn is active — the architectural fix for the notify_on_complete wakeup
// gap that #2242 + #2279 papered over.
//
// Lifecycle: opened on session mount (loadSession / newSession), closed on
// session switch / unmount. The browser closes it implicitly on tab close
// (server detects disconnect via the SSE read-loop and unsubscribes).
let _sessionEventSource = null;
let _sessionStreamSessionId = null;
let _sessionStreamReconnectTimer = null;
// Holds the session id across a hidden-tab close so the visibility handler can
// reopen the per-session SSE on re-show (stopSessionStream nulls _sessionStreamSessionId).
let _sessionStreamHiddenSid = null;

function startSessionStream(sid) {
  if (!sid) return;
  // Already on this session? No-op (loadSession is a no-op when re-selecting
  // the same session; this defends against external re-callers).
  if (_sessionStreamSessionId === sid && _sessionEventSource) return;
  stopSessionStream();
  _sessionStreamSessionId = sid;
  // Visibility hook (install once) — mirror ensureSessionEventsSSE() pattern.
  // Capture the active session id into a dedicated var BEFORE closing, because
  // stopSessionStream() nulls _sessionStreamSessionId — so the reopen path can't
  // rely on it (that was the bug: the stream never reopened on tab re-show).
  if (typeof document !== 'undefined' && !document._hermesSessionStreamVisibilityHook) {
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        _sessionStreamHiddenSid = _sessionStreamSessionId;
        stopSessionStream();
      } else if (_sessionStreamHiddenSid) {
        const resumeSid = _sessionStreamHiddenSid;
        _sessionStreamHiddenSid = null;
        void startSessionStream(resumeSid);
      }
    });
    document._hermesSessionStreamVisibilityHook = true;
  }
  // Don't open when tab is hidden — saves connection pool slots. Preserve the
  // pending session id so the visibility handler reopens it on re-show (a session
  // loaded/restored while the tab is already hidden must still reattach).
  if (typeof document !== 'undefined' && document.hidden) {
    _sessionStreamHiddenSid = sid;
    return;
  }
  try {
    const es = new EventSource(_apiUrl('api/session/stream?session_id=' + encodeURIComponent(sid)));
    _sessionEventSource = es;
    es.addEventListener('initial', () => { /* connection confirmed */ });
    es.addEventListener('bg_task_complete', e => {
      // Shared handler — same dedupe set as the in-turn STREAMS path.
      if (typeof _handleBgTaskCompleteEvent === 'function') {
        _handleBgTaskCompleteEvent(e, sid, {source: 'session'});
      }
    });
    // ── Defect B: live-view of server-initiated (Option Z) turns ──────────
    // The drain thread starts the wakeup turn server-side and the server
    // fans a `server_turn_started` {stream_id} frame onto this per-session
    // channel. No browser POSTed /api/chat/start, so nothing is attached to
    // that STREAMS[stream_id] yet. Attach the EXISTING chat-stream renderer
    // (attachLiveStream — the exact path /api/chat/start uses) to the
    // server-created stream so the open tab renders the turn live. Reuses
    // the one renderer; does NOT hand-roll a second one.
    es.addEventListener('server_turn_started', e => {
      try {
        const d = JSON.parse(e.data || '{}');
        const evSid = d.session_id || sid;
        const streamId = String(d.stream_id || '');
        if (!streamId || evSid !== sid) return;
        // `recovered` marks an on-subscribe replay from the server: the tab
        // (re)connected to /api/session/stream AFTER the original
        // fire-and-forget server_turn_started had already been broadcast, so
        // the live stream is mid-flight. Attach via the reconnecting (replay)
        // path so the renderer rebuilds from the run journal instead of
        // expecting token 0 (which would render a truncated turn). A fresh
        // (non-recovered) frame still attaches from the first token.
        const recovered = !!d.recovered;
        // Only drive the renderer when this session is the one on screen.
        const isCurrent = (typeof _isSessionCurrentPane === 'function')
          ? _isSessionCurrentPane(sid)
          : (S.session && S.session.session_id === sid);
        if (!isCurrent) return;
        // A turn is already rendering in this tab (user-initiated, or we
        // already attached to this very stream). attachLiveStream is
        // idempotent per (sid, streamId); bail if we're already on it.
        if (S.activeStreamId === streamId) return;
        const existingLive = (typeof LIVE_STREAMS !== 'undefined') ? LIVE_STREAMS[sid] : null;
        if (existingLive && existingLive.streamId === streamId) return;
        // Mirror the loadSession reattach setup. For a fresh frame the turn
        // renders from its first token; for a recovered (replay) frame
        // attachLiveStream reconstructs the in-progress stream.
        S.busy = true;
        S.activeStreamId = streamId;
        if (S.session && S.session.session_id === sid) {
          S.session.active_stream_id = streamId;
          if (typeof d.pending_started_at === 'number') S.session.pending_started_at = d.pending_started_at;
          else if (!S.session.pending_started_at) S.session.pending_started_at = Date.now()/1000;
        }
        if (typeof ensureLiveWorklogShell === 'function') ensureLiveWorklogShell();
        else if (typeof appendThinking === 'function') appendThinking();
        if (typeof updateSendBtn === 'function') updateSendBtn();
        if (typeof setComposerStatus === 'function') setComposerStatus('');
        if (typeof syncTopbar === 'function') syncTopbar();
        if (typeof startApprovalPolling === 'function') startApprovalPolling(sid);
        if (typeof startClarifyPolling === 'function') startClarifyPolling(sid);
        if (typeof attachLiveStream === 'function') {
          attachLiveStream(
            sid, streamId,
            (S.session && S.session.pending_attachments) || [],
            recovered ? {reconnecting: true} : {},
          );
        }
        if (typeof renderSessionList === 'function') void renderSessionList();
      } catch (_) {}
    });
    es.onerror = () => {
      // Browser already auto-reconnects EventSource on most transient
      // failures. We only intervene if the connection has been closed for
      // good (readyState === 2) — schedule a one-shot re-open after 5s.
      if (es.readyState === 2 && _sessionStreamSessionId === sid) {
        if (_sessionStreamReconnectTimer) clearTimeout(_sessionStreamReconnectTimer);
        // The CLOSED EventSource (readyState === 2) will never reconnect on
        // its own, and startSessionStream's top guard
        // (`_sessionStreamSessionId === sid && _sessionEventSource`) would
        // short-circuit the re-open while this dead object is still pinned.
        // Drop our reference (and close it for good measure) so the timer's
        // startSessionStream() reaches stopSessionStream() and builds a FRESH
        // EventSource instead of reusing the closed one. Only clear if `es`
        // is still the active source — a newer connection may have replaced
        // it in the interim (stale onerror from a superseded stream), in
        // which case we must not stomp the live one.
        if (_sessionEventSource === es) {
          try { if(es.readyState!==2)es.close(); } catch (_) {}
          _sessionEventSource = null;
        }
        _sessionStreamReconnectTimer = setTimeout(() => {
          _sessionStreamReconnectTimer = null;
          if (_sessionStreamSessionId === sid) startSessionStream(sid);
        }, 5000);
      }
    };
  } catch(_) {
    // EventSource ctor threw — silently disabled; the in-turn STREAMS path
    // still works for events that fire during an active turn.
    _sessionEventSource = null;
  }
}

function stopSessionStream() {
  if (_sessionStreamReconnectTimer) { clearTimeout(_sessionStreamReconnectTimer); _sessionStreamReconnectTimer = null; }
  if (_sessionEventSource) {
    try { if(_sessionEventSource.readyState!==2)_sessionEventSource.close(); } catch(_){}
    _sessionEventSource = null;
  }
  _sessionStreamSessionId = null;
}

// Shared bg_task_complete handler — invoked from BOTH the in-turn STREAMS
// channel (legacy path, still kept as defense-in-depth) AND the session-
// scoped channel (Option X primary path). Dedupes by (session_id, event_id)
// via the Map+TTL ring buffer declared at the top of this module.
// Events without `event_id` are ignored — the server contract guarantees one
// on every completion emit, so a missing key signals a malformed or replayed
// payload we should not surface or ack.
// PR (c) UX surface: post-dedupe the handler marks the session viewed (when
// the session pane is current and the doc is visible+focused), then runs the
// T4 drop-when-focused gate; only out-of-focus or off-pane completions spawn
// a toast. The diagnostic ack POST still fires for both focused and
// unfocused viewers so the server receives the delivery/cleanup signal;
// the focus gate suppresses UI noise only.
function _handleBgTaskCompleteEvent(e, expectedSid, opts) {
  try {
    const d = JSON.parse(e.data || '{}');
    const sid = d.session_id || expectedSid;
    if (sid !== expectedSid) return;
    const evt_id = d.event_id ? String(d.event_id) : '';
    if (!evt_id) return;  // server contract requires event_id; ignore otherwise
    if (_bgTaskCompleteRingBufferAdd(sid, evt_id)) return;  // duplicate
    const pid = String(d.task_id || '');
    const _viewed = typeof _isSessionActivelyViewed === 'function' && _isSessionActivelyViewed(sid);
    if (_viewed) {
      try { _markSessionViewed(sid, (S&&S.session&&S.session.session_id===sid)?(S.session.message_count??(S.messages&&S.messages.length)??0):0); } catch(_){}
      try { if(typeof _clearSessionCompletionUnread==='function') _clearSessionCompletionUnread(sid); } catch(_){}
    } else {
      // T4 drop-when-focused: suppress toast only; ack below still fires.
      try {
        const tid = (d.task_id || '').slice(0, 8) || '?';
        const tail = d.summary ? `: ${String(d.summary).slice(0, 80)}` : '';
        showToast(`Task ${tid} done${tail}`, 2600);
      } catch (_) {}
    }

    // Fire-and-forget ack (diagnostic only — Option Z made this a no-op for
    // state. The agent wakeup is now started SERVER-SIDE by the drain thread
    // in api/background_process._process_one → start_session_turn; the
    // browser is no longer in the wakeup path at all.)
    try {
      fetch(_apiUrl('api/bg-task-complete-ack'), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'include',
        body: JSON.stringify({session_id: sid, task_id: pid, event_id: evt_id}),
      }).catch(() => {});
    } catch(_) {}

    // Option Z PIVOT: the browser NO LONGER re-POSTs the chat-start endpoint
    // to wake the agent. Server-side wakeup is the PRIMARY mechanism — the
    // drain thread starts the turn directly (no tab required), so the
    // closed-tab case works (parity with CLI/Telegram). The per-session SSE
    // channel this handler is wired into is DEMOTED to a pure live-view
    // layer: if a tab is open the server-initiated turn streams live via the
    // existing chat-stream EventSource; if the tab is closed the turn still
    // runs server-side and the result is persisted to the session store.
    // The user-facing toast + drop-when-focused gate land in PR (c).
  } catch(_) {}
}

// ── Clarify polling ──
let _clarifyPollTimer = null;
let _clarifyHideTimer = null;
let _clarifyVisibleSince = 0;
let _clarifySignature = '';
let _clarifySessionId = null;
let _clarifyId = null;
let _clarifyMissingEndpointWarned = false;
let _clarifyCountdownTimer = null;
let _clarifyExpiresAt = 0;
let _clarifyPendingBySession = new Map();
const CLARIFY_MIN_VISIBLE_MS = 30000;

function _clarifyPromptBelongsToActiveSession(sid) {
  return !!(sid && _promptActiveSessionId() === sid);
}

function _rememberClarifyPending(pending) {
  if (!pending) return null;
  const sid = pending._session_id || _promptActiveSessionId();
  if (!sid) return null;
  const nextPending = {...pending, _session_id: sid};
  _clarifyPendingBySession.set(sid, {pending: nextPending});
  return sid;
}

function _clearClarifyPendingForSession(sid) {
  if (sid) {
    _clarifyPendingBySession.delete(sid);
    if (typeof syncTopbar === 'function') syncTopbar();
  }
}

function _hideClarifyCardIfOwner(sid, force=false, reason="dismissed") {
  if (!sid || _clarifySessionId === sid) hideClarifyCard(force, reason);
}

function _renderPendingClarifyForActiveSession() {
  const sid = _promptActiveSessionId();
  if (!sid) return;
  if (_clarifySessionId && _clarifySessionId !== sid) hideClarifyCard(true, 'session');
  const entry = _clarifyPendingBySession.get(sid);
  if (entry) showClarifyCard(entry.pending);
}

function showClarifyForSession(sid, pending) {
  if (!pending) return;
  pending._session_id = sid;
  showClarifyCard(pending);
}

function _renderPendingPromptsForActiveSession() {
  const sid = _promptActiveSessionId();
  _renderPendingApprovalForActiveSession();
  _renderPendingClarifyForActiveSession();
  if (
    sid &&
    typeof activeSessionHasPendingPromptAttention === 'function' &&
    activeSessionHasPendingPromptAttention()
  ) return;
  if (typeof syncTopbar === 'function') syncTopbar();
}

function _ensureClarifyCardDom() {
  let card = $("clarifyCard");
  if (card) return card;
  const host = $("msgInner") || $("messages");
  if (!host) return null;
  card = document.createElement("div");
  card.className = "clarify-card";
  card.id = "clarifyCard";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-labelledby", "clarifyHeading");
  card.setAttribute("aria-describedby", "clarifyQuestion clarifyHint");
  card.innerHTML = `
    <div class="clarify-inner">
      <div class="clarify-header">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 17h.01"/><path d="M9.09 9a3 3 0 1 1 5.82 1c0 2-3 2-3 4"/><circle cx="12" cy="12" r="10"/></svg>
        <span id="clarifyHeading" data-i18n="clarify_heading">Clarification needed</span>
        <span class="clarify-countdown" id="clarifyCountdown"></span>
        <button type="button" class="clarify-collapse" id="clarifyCollapse" aria-expanded="true" aria-label="Collapse clarification" aria-controls="clarifyQuestion clarifyChoices clarifyInput clarifyHint" onclick="toggleClarifyCardCollapsed()" title="Collapse clarification"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"></polyline></svg></button>
      </div>
      <div class="clarify-question" id="clarifyQuestion"></div>
      <div class="clarify-choices" id="clarifyChoices"></div>
      <div class="clarify-response">
        <input class="clarify-input" id="clarifyInput" type="text" autocomplete="off" readonly onfocus="this.removeAttribute('readonly')" data-i18n-placeholder="clarify_input_placeholder" placeholder="Type your response…">
        <button class="clarify-submit" id="clarifySubmit" data-i18n="clarify_send">Send</button>
      </div>
      <div class="clarify-hint" id="clarifyHint" data-i18n="clarify_hint">Please choose one option, or type your own response below.</div>
    </div>
  `;
  host.appendChild(card);
  const submit = $("clarifySubmit");
  if (submit) submit.onclick = () => respondClarify();
  const collapse = $("clarifyCollapse");
  if (collapse) collapse.onclick = () => toggleClarifyCardCollapsed();
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  return card;
}

function _syncClarifyCollapseButton(card) {
  const collapse = $("clarifyCollapse");
  if (!collapse || !card) return;
  const collapsed = card.classList.contains("collapsed");
  collapse.setAttribute("aria-expanded", collapsed ? "false" : "true");
  // Icon swap: chevron-down when expanded (click to collapse), chevron-up when collapsed (click to expand)
  const polyline = collapse.querySelector("svg polyline");
  if (polyline) polyline.setAttribute("points", collapsed ? "18 15 12 9 6 15" : "6 9 12 15 18 9");
  const label = collapsed ? "Expand clarification" : "Collapse clarification";
  collapse.setAttribute("aria-label", label);
  collapse.title = label;
}

let _clarifyResizeListenerReady = false;

function _clarifyMessagesNearBottom(messages) {
  if (!messages) return false;
  return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 150;
}

function _syncClarifyTranscriptSpace(card, opts) {
  opts = opts || {};
  const messages = $("messages");
  if (!messages) return;
  const wasNearBottom = _clarifyMessagesNearBottom(messages);
  if (!card || !card.classList.contains("visible")) {
    messages.classList.remove("clarify-open");
    messages.classList.remove("clarify-collapsed");
    messages.style.removeProperty("--clarify-card-height");
    messages.style.removeProperty("--clarify-dock-height");
    if (wasNearBottom && typeof scrollToBottom === "function" && typeof requestAnimationFrame === "function") {
      requestAnimationFrame(scrollToBottom);
    }
    return;
  }
  const collapsed = card.classList.contains("collapsed");
  messages.classList.add("clarify-open");
  messages.classList.toggle("clarify-collapsed", collapsed);
  const measure = () => {
    if (!card.classList.contains("visible")) return;
    const target = collapsed ? card : (card.querySelector(".clarify-inner") || card);
    const h = target && target.getBoundingClientRect().height;
    if (h > 0) {
      messages.style.setProperty(collapsed ? "--clarify-dock-height" : "--clarify-card-height", Math.ceil(h + 24) + "px");
    }
    if (wasNearBottom && typeof scrollToBottom === "function") scrollToBottom();
  };
  if (opts.immediate) measure();
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(measure);
  setTimeout(measure, 420);
}

function _ensureClarifyResizeListener() {
  if (_clarifyResizeListenerReady || typeof window === "undefined") return;
  _clarifyResizeListenerReady = true;
  window.addEventListener("resize", () => {
    const card = $("clarifyCard");
    if (card && card.classList.contains("visible")) {
      _syncClarifyTranscriptSpace(card, {immediate: true});
    }
  }, {passive: true});
}

function toggleClarifyCardCollapsed(forceCollapsed) {
  const card = $("clarifyCard");
  if (!card) return;
  const collapsed = typeof forceCollapsed === "boolean" ? forceCollapsed : !card.classList.contains("collapsed");
  card.classList.toggle("collapsed", collapsed);
  _syncClarifyCollapseButton(card);
  _syncClarifyTranscriptSpace(card, {immediate: true});
}

function _clearClarifyHideTimer() {
  if (_clarifyHideTimer) {
    clearTimeout(_clarifyHideTimer);
    _clarifyHideTimer = null;
  }
}

function _clearClarifyCountdownTimer() {
  if (_clarifyCountdownTimer) {
    clearInterval(_clarifyCountdownTimer);
    _clarifyCountdownTimer = null;
  }
  _clarifyExpiresAt = 0;
  const countdown = $("clarifyCountdown");
  if (countdown) {
    countdown.textContent = "";
    countdown.classList.remove("urgent");
  }
}

function _clarifyExpiryMs(pending) {
  const expiresAt = Number(pending && pending.expires_at);
  if (Number.isFinite(expiresAt) && expiresAt > 0) return expiresAt * 1000;
  const requestedAt = Number(pending && pending.requested_at);
  const timeoutSeconds = Number(pending && pending.timeout_seconds);
  if (Number.isFinite(requestedAt) && Number.isFinite(timeoutSeconds)) {
    return (requestedAt + timeoutSeconds) * 1000;
  }
  return 0;
}

function _updateClarifyCountdown() {
  const countdown = $("clarifyCountdown");
  if (!countdown || !_clarifyExpiresAt) return;
  const remaining = Math.max(0, Math.ceil((_clarifyExpiresAt - Date.now()) / 1000));
  countdown.textContent = `${remaining}s`;
  countdown.classList.toggle("urgent", remaining <= 10);
}

function _startClarifyCountdown(pending) {
  const expiresAt = _clarifyExpiryMs(pending);
  if (_clarifyCountdownTimer && _clarifyExpiresAt === expiresAt) return;
  _clearClarifyCountdownTimer();
  _clarifyExpiresAt = expiresAt;
  if (!_clarifyExpiresAt) return;
  _updateClarifyCountdown();
  _clarifyCountdownTimer = setInterval(_updateClarifyCountdown, 1000);
}

function _stashClarifyDraft(reason) {
  if (reason !== "expired" && reason !== "terminal") return false;
  const submit = $("clarifySubmit");
  if (submit && submit.classList.contains("loading")) return false;
  const input = $("clarifyInput");
  const draft = String((input && input.value) || "").trim();
  if (!draft) return false;
  const sid = _clarifySessionId || (S.session && S.session.session_id) || "unknown";
  const key = `hermes-clarify-draft-${sid}-${_clarifySignature || "unknown"}`;
  try {
    sessionStorage.setItem(key, JSON.stringify({
      draft,
      reason,
      saved_at: Date.now(),
    }));
  } catch (_) {}
  const composer = $('msg');
  if (composer) {
    const current = String(composer.value || "");
    composer.value = current.trim() ? `${current.replace(/\s+$/, "")}\n\n${draft}` : draft;
    if (typeof autoResize === "function") autoResize();
    if (typeof updateSendBtn === "function") updateSendBtn();
  }
  const notice = reason === "expired"
    ? "Clarification timed out. Your draft was kept in the composer."
    : "Clarification closed. Your draft was kept in the composer.";
  if (typeof setComposerStatus === "function") setComposerStatus(notice);
  else if (typeof setStatus === "function") setStatus(notice);
  if (typeof showToast === "function") showToast(notice, 5000);
  return true;
}

function _resetClarifyCardState() {
  _clearClarifyHideTimer();
  _clearClarifyCountdownTimer();
  _clarifyVisibleSince = 0;
  _clarifySignature = '';
  _clarifyId = null;
}

function hideClarifyCard(force=false, reason="dismissed") {
  const card = $("clarifyCard");
  if (!card) {
    _clarifySessionId = null;
    _resetClarifyCardState();
    if (typeof unlockComposerForClarify === "function") unlockComposerForClarify();
    return;
  }
  if (!force && reason !== "expired" && _clarifyVisibleSince) {
    const remaining = CLARIFY_MIN_VISIBLE_MS - (Date.now() - _clarifyVisibleSince);
    if (remaining > 0) {
      const scheduledSignature = _clarifySignature;
      _clearClarifyHideTimer();
      _clarifyHideTimer = setTimeout(() => {
        _clarifyHideTimer = null;
        if (_clarifySignature !== scheduledSignature) return;
        hideClarifyCard(true, reason);
      }, remaining);
      return;
    }
  }
  _stashClarifyDraft(reason);
  _clarifySessionId = null;
  _resetClarifyCardState();
  card.classList.remove("visible");
  _syncClarifyTranscriptSpace(null);
  if (typeof unlockComposerForClarify === "function") unlockComposerForClarify();
  $("clarifyQuestion").textContent = "";
  $("clarifyChoices").innerHTML = "";
  $("clarifyInput").value = "";
  $("clarifyInput").disabled = false;
  $("clarifyInput").onkeydown = null;
  const submit = $("clarifySubmit");
  if (submit) { submit.disabled = false; submit.classList.remove("loading"); }
}

function _clarifySetControlsDisabled(disabled, loading=false) {
  const input = $("clarifyInput");
  const submit = $("clarifySubmit");
  if (input) input.disabled = disabled;
  if (submit) {
    submit.disabled = disabled;
    submit.classList.toggle("loading", !!loading);
  }
  const choices = $("clarifyChoices");
  if (choices) {
    choices.querySelectorAll("button").forEach(btn => {
      btn.disabled = disabled;
      if (loading && btn.dataset && btn.dataset.choice === "other") {
        btn.classList.toggle("loading", false);
      }
    });
  }
}

function showClarifyCard(pending) {
  const sid = _rememberClarifyPending(pending);
  if (!_clarifyPromptBelongsToActiveSession(sid)) return;
  const question = pending.question || pending.description || '';
  const choices = Array.isArray(pending.choices_offered)
    ? pending.choices_offered
    : (Array.isArray(pending.choices) ? pending.choices : []);
  const sig = JSON.stringify({
    question,
    choices,
    sid: pending._session_id || (S.session && S.session.session_id) || null,
    clarify_id: pending.clarify_id || null,
  });
  const card = _ensureClarifyCardDom();
  if (!card) return;
  const questionEl = $("clarifyQuestion");
  const choicesEl = $("clarifyChoices");
  const input = $("clarifyInput");
  const sameClarify = card.classList.contains("visible") && _clarifySignature === sig;
  _clarifySessionId = sid;
  _clarifyId = pending.clarify_id || null;
  _clarifySignature = sig;
  _startClarifyCountdown(pending);
  if (!sameClarify) {
    _clarifyVisibleSince = Date.now();
    _clearClarifyHideTimer();
    card.classList.remove("collapsed");
  }
  if (questionEl) questionEl.textContent = question;
  if (choicesEl) {
    choicesEl.innerHTML = '';
    choicesEl.style.display = choices.length ? '' : 'none';
    if (choices.length) {
      choices.forEach((choice, idx) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'clarify-choice';
        btn.dataset.choice = choice;
        btn.onclick = () => respondClarify(choice);
        const badge = document.createElement('span');
        badge.className = 'clarify-choice-badge';
        badge.textContent = String(idx + 1);
        const text = document.createElement('span');
        text.className = 'clarify-choice-text';
        text.textContent = choice;
        btn.appendChild(badge);
        btn.appendChild(text);
        choicesEl.appendChild(btn);
      });
      const other = document.createElement('button');
      other.type = 'button';
      other.className = 'clarify-choice other';
      other.dataset.choice = 'other';
      other.setAttribute('data-i18n', 'clarify_other');
      const otherBadge = document.createElement('span');
      otherBadge.className = 'clarify-choice-badge other';
      otherBadge.textContent = '•';
      const otherText = document.createElement('span');
      otherText.className = 'clarify-choice-text';
      otherText.textContent = t('clarify_other') || 'Other';
      other.appendChild(otherBadge);
      other.appendChild(otherText);
      other.onclick = () => {
        const el = $("clarifyInput");
        if (el) {
          el.focus();
          if (typeof el.select === 'function') el.select();
        }
      };
      choicesEl.appendChild(other);
    }
  }
  if (input) {
    if (!sameClarify) input.value = '';
    input.disabled = false;
    input.removeAttribute('readonly');
    input.onkeydown = (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        respondClarify();
      }
    };
  }
  if (typeof lockComposerForClarify === "function") {
    lockComposerForClarify(question ? `Clarification needed: ${question}` : "Clarification needed");
  }
  _clarifySetControlsDisabled(false, false);
  _ensureClarifyResizeListener();
  card.classList.add("visible");
  _syncClarifyCollapseButton(card);
  _syncClarifyTranscriptSpace(card, {immediate: true});
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  // Move focus to clarify input synchronously (not in setTimeout) and
  // only if the user wasn't mid-type in the composer textarea.
  if (input && !sameClarify && document.activeElement !== $('msg')) {
    input.focus({preventScroll: true});
  }
  if (typeof syncTopbar === 'function') syncTopbar();
}

async function respondClarify(response) {
  const sid = _clarifySessionId || (S.session && S.session.session_id);
  if (!sid) return;
  const input = $("clarifyInput");
  let value = typeof response === 'string' ? response : (input ? input.value : '');
  value = String(value || '').trim();
  if (!value) {
    if (input) input.focus();
    return;
  }
  const clarifyId = _clarifyId;
  // Keep a draft copy so we can restore the input on failure (issue #2639).
  const draft = value;
  _clarifySetControlsDisabled(true, true);
  try {
    const result = await api("/api/clarify/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: sid, response: value, clarify_id: clarifyId || "" })
    });
    if (result && result.ok) {
      // Only clear/hide if the visible prompt still matches what was just
      // submitted.  If a parallel SSE event already loaded the next queued
      // prompt, erasing the session cache would leave the agent waiting
      // until timeout (codex review P1, issue #2639).
      if (_clarifyId === clarifyId) {
        _clarifySessionId = null;
        _clarifyId = null;
        _clearClarifyPendingForSession(sid);
        hideClarifyCard(true, 'sent');
        // Echo the user's clarify choice as a visible message in the conversation
        if (S.session && S.session.session_id === sid) {
          S.messages.push({
            role: 'user',
            content: value,
            _clarify_response: true,
            _ts: Date.now() / 1000,
          });
          if (typeof renderMessages === 'function') renderMessages({preserveScroll: true});
        }
      }
    } else {
      // Stale / expired / wrong session — keep the card and draft visible.
      _clarifySetControlsDisabled(false, false);
      if (input) {
        input.value = draft;
        input.focus();
      }
      const errMsg = (result && result.error) || "Clarification response not accepted — the agent may have already proceeded.";
      if (typeof showToast === "function") showToast(errMsg, 5000);
      if (typeof setStatus === "function") setStatus(errMsg);
    }
  } catch(e) {
    // The server returns 409 with ``stale: true`` for both genuinely-expired
    // prompts and wrong-session/next-prompt-loaded races. In both cases the
    // server-side ``_pending`` entry for *this* clarify_id is gone, so
    // retrying it can never succeed — the prior keep-card-and-draft
    // behavior left the user with a permanently 409-ing card and a locked
    // composer, with no affordance to dismiss it (#4504). Treat 409 as
    // terminal here, but only when the visible card still matches what we
    // just submitted: mirroring the success path's ``_clarifyId === clarifyId``
    // guard (codex review P1, #2639) — if a parallel poll already rendered
    // the *next* queued prompt B while A's response was in flight, we must
    // not tear B down on A's late 409. The SSE/poll path will re-render the
    // next prompt's card from scratch via ``showClarifyCard`` either way.
    if (e && e.status === 409) {
      if (_clarifyId === clarifyId) {
        // Same card still showing — dismiss it and rescue the typed draft.
        // Order matters: ``_stashClarifyDraft`` (called from
        // ``hideClarifyCard``) bails when ``#clarifySubmit`` still carries
        // the ``loading`` class set above. Clear loading first, otherwise
        // the typed answer is silently dropped (reviewer P1).
        _clarifySetControlsDisabled(false, false);
        _clarifySessionId = null;
        _clarifyId = null;
        _clearClarifyPendingForSession(sid);
        hideClarifyCard(true, "expired");
        const errMsg = (e.message || "Clarification prompt expired or not found.");
        if (typeof setStatus === "function") setStatus("Clarify: " + errMsg);
        // ``_stashClarifyDraft('expired')`` already surfaces the actionable
        // "Clarification timed out. Your draft was kept in the composer."
        // toast when there is a draft to rescue, so we don't double-toast.
        return;
      }
      // A newer prompt is showing (race between user click and SSE/poll).
      // Don't dismiss it on this late 409 — just re-enable controls and
      // surface the error. The user's draft for the now-stale prompt is
      // dropped intentionally; the next prompt has its own input cycle.
      _clarifySetControlsDisabled(false, false);
      if (typeof setStatus === "function") {
        setStatus("Clarify: previous prompt expired — a newer one is showing.");
      }
      return;
    }
    // Network / other transient errors — keep the card and draft visible so
    // the user can retry once connectivity returns.
    _clarifySetControlsDisabled(false, false);
    if (input) {
      input.value = draft;
      input.focus();
    }
    const errMsg = (e && e.message) || "Failed to deliver clarification response.";
    if (typeof setStatus === "function") setStatus("Clarify: " + errMsg);
    if (typeof showToast === "function") showToast(errMsg, 5000);
  }
}

var _clarifyEventSource = null;
var _clarifyFallbackTimer = null;
var _clarifyHealthTimer = null;
let _clarifyFallbackPollInFlight = false;
let _clarifyPollingSessionId = null;

function startClarifyPolling(sid) {
  stopClarifyPolling();
  _clarifyPollingSessionId = sid || null;
  _clarifyMissingEndpointWarned = false;

  // Use HTTP polling instead of SSE to avoid browser connection pool exhaustion.
  // Browsers limit to 6 concurrent HTTP connections per origin over HTTP/1.1.
  // With 6 persistent SSE streams (sessions/events, gateway/stream,
  // session/stream, approval/stream, clarify/stream, chat/stream), the pool
  // fills and all fetch() requests queue indefinitely. The server responds
  // normally (curl works), but the browser has no available sockets.
  //
  // This was introduced in v0.51.340 when /api/session/stream was added as
  // the 6th persistent SSE connection. Until we multiplex streams or serve
  // SSE from a separate origin, use HTTP polling to free 2 connection slots.
  // (3-second interval, acceptable tradeoff)
  _startClarifyFallbackPoll(sid);
}

function _startClarifyFallbackPoll(sid) {
  _clarifyPollingSessionId = sid || null;
  // Run one tick immediately so a session already blocked on a pending clarify
  // shows its card instantly (the removed SSE 'initial' event used to do this);
  // then poll on the 3000ms cadence. (#3913 SHOULD-FIX)
  const _tick = async () => {
    if (!S.session || S.session.session_id !== sid) {
      stopClarifyPolling(); _hideClarifyCardIfOwner(sid, true, 'session'); return;
    }
    if (_clarifyFallbackPollInFlight) return;
    _clarifyFallbackPollInFlight = true;
    try {
      const data = await api("/api/clarify/pending?session_id=" + encodeURIComponent(sid),{timeoutToast:false});
      if (data.pending) { showClarifyForSession(sid, data.pending); }
      else { _clearClarifyPendingForSession(sid); _hideClarifyCardIfOwner(sid, false, 'expired'); }
    } catch(e) {
      const msg = String((e && e.message) || "");
      if (!_clarifyMissingEndpointWarned && /(^|\b)(404|not found)(\b|$)/i.test(msg)) {
        _clarifyMissingEndpointWarned = true;
        setComposerStatus("Clarify unavailable on current server build. Restart server.");
        if (typeof showToast === "function") {
          showToast("Clarify endpoint unavailable. Please restart server.", 5000);
        }
        stopClarifyPolling();
      }
    } finally {
      _clarifyFallbackPollInFlight = false;
    }
  };
  _clarifyFallbackTimer = setInterval(_tick, 3000);
  _tick();
}

function stopClarifyPollingForSession(sid) {
  if(sid && _clarifyPollingSessionId && _clarifyPollingSessionId!==sid) return;
  stopClarifyPolling();
}

function stopClarifyPolling() {
  if (_clarifyEventSource) { try { if(_clarifyEventSource.readyState!==2)_clarifyEventSource.close(); } catch(_){} _clarifyEventSource = null; }
  if (_clarifyFallbackTimer) { clearInterval(_clarifyFallbackTimer); _clarifyFallbackTimer = null; }
  if (_clarifyHealthTimer) { clearInterval(_clarifyHealthTimer); _clarifyHealthTimer = null; }
  _clarifyFallbackPollInFlight = false;
  _clarifyPollingSessionId = null;
}

// ── Notifications and Sound ──────────────────────────────────────────────────

function playNotificationSound(){
  if(!window._soundEnabled) return;
  try{
    const ctx=new (window.AudioContext||window.webkitAudioContext)();
    const osc=ctx.createOscillator();
    const gain=ctx.createGain();
    osc.connect(gain);gain.connect(ctx.destination);
    osc.type='sine';osc.frequency.setValueAtTime(660,ctx.currentTime);
    osc.frequency.setValueAtTime(880,ctx.currentTime+0.1);
    gain.gain.setValueAtTime(0.3,ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01,ctx.currentTime+0.3);
    osc.start(ctx.currentTime);osc.stop(ctx.currentTime+0.3);
    osc.onended=()=>ctx.close();
  }catch(e){console.warn('Notification sound failed:',e);}
}


function _attentionSoundKey(sid,kind,count){
  const safeSid=String(sid||'');
  const safeKind=String(kind||'attention');
  const safeCount=Math.max(1,Number(count)||1);
  return `${safeSid}:${safeKind}:${safeCount}`;
}

function playAttentionSound(key){
  if(!window._soundEnabled) return;
  const nowMs=Date.now();
  if(window._lastAttentionSoundAt&&nowMs-window._lastAttentionSoundAt<900) return;
  const dedupeKey=key?String(key):'';
  if(dedupeKey){
    const seen=window._attentionSoundSeenKeys instanceof Map?window._attentionSoundSeenKeys:new Map();
    window._attentionSoundSeenKeys=seen;
    for(const [seenKey,seenAt] of seen){
      if(nowMs-Number(seenAt||0)>300000) seen.delete(seenKey);
    }
    if(seen.has(dedupeKey)) return;
    seen.set(dedupeKey,nowMs);
  }
  window._lastAttentionSoundAt=nowMs;
  try{
    const ctx=new (window.AudioContext||window.webkitAudioContext)();
    const osc=ctx.createOscillator();
    const gain=ctx.createGain();
    osc.connect(gain);gain.connect(ctx.destination);
    osc.type='sine';osc.frequency.setValueAtTime(880,ctx.currentTime);
    osc.frequency.setValueAtTime(660,ctx.currentTime+0.075);
    gain.gain.setValueAtTime(0.24,ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01,ctx.currentTime+0.24);
    osc.start(ctx.currentTime);osc.stop(ctx.currentTime+0.24);
    osc.onended=()=>ctx.close();
  }catch(e){console.warn('Attention sound failed:',e);}
}

function _notificationOptions(body,options={}){
  const sid=(options&&options.sid)||(S&&S.session&&S.session.session_id);
  const url=sid?`${location.origin}${_sessionUrlForSid(sid)}`:location.href;
  return {body:body||'',tag:sid?`hermes-${sid}`:'hermes-webui',renotify:false,icon:'static/favicon-192.png',badge:'static/favicon-32.png',data:{url}};
}
function _showPwaNotification(title,body,options={}){
  const botName=assistantDisplayName();
  const opts=_notificationOptions(body,options);
  const direct=()=>new Notification(title||botName,opts);
  // Prefer the service worker (the only path that works in a standalone PWA,
  // notably iOS). Use getRegistration() + a short timeout race rather than
  // navigator.serviceWorker.ready, because `.ready` NEVER settles when no
  // registration ever activates for the scope (e.g. a reverse proxy serving
  // sw.js with the wrong MIME type, or SW disabled in the browser) — which
  // would silently drop every notification instead of falling back.
  if(navigator.serviceWorker&&navigator.serviceWorker.getRegistration){
    const reg$=Promise.race([
      navigator.serviceWorker.getRegistration().catch(()=>null),
      new Promise(res=>setTimeout(()=>res(null),2000))
    ]);
    return reg$.then(reg=>(reg&&reg.active&&reg.showNotification)
      ? reg.showNotification(title||botName,opts)
      : direct());
  }
  return Promise.resolve(direct());
}
function requestNotificationPermission(){
  if(!('Notification' in window)){
    if(typeof showToast==='function') showToast(t('notifications_unsupported'),3000,'error');
    if(typeof updateNotificationPermissionStatus==='function') updateNotificationPermissionStatus();
    return Promise.resolve('unsupported');
  }
  if(Notification.permission==='granted'){
    if(typeof updateNotificationPermissionStatus==='function') updateNotificationPermissionStatus();
    if(typeof showToast==='function') showToast(t('notifications_enabled_toast'),3000);
    return Promise.resolve('granted');
  }
  if(Notification.permission==='denied'){
    if(typeof showToast==='function') showToast(t('notifications_denied'),3500,'error');
    if(typeof updateNotificationPermissionStatus==='function') updateNotificationPermissionStatus();
    return Promise.resolve('denied');
  }
  return Notification.requestPermission().then(p=>{
    if(typeof showToast==='function') showToast(p==='granted'?t('notifications_enabled_toast'):t('notifications_denied'),3000,p==='granted'?undefined:'error');
    if(typeof updateNotificationPermissionStatus==='function') updateNotificationPermissionStatus();
    return p;
  });
}
function sendBrowserNotification(title,body,options={}){
  const force=!!(options&&options.force);
  // #4416: `forceHidden` means the caller already determined the tab was hidden
  // during the relevant window (e.g. a stream that ran while backgrounded), so
  // the live `document.hidden` visibility gate — which a late, throttled SSE
  // makes unreliable — should be treated as satisfied. The user's
  // notifications-enabled SETTING is still honored (unlike `force`, which is the
  // explicit "Send test" override); only the visibility gate is bypassed.
  const forceHidden=!!(options&&options.forceHidden);
  if(!force&&!window._notificationsEnabled) return;
  if(!force&&!forceHidden&&!document.hidden) return;
  if(!('Notification' in window)) return;
  if(Notification.permission==='granted'){
    _showPwaNotification(title,body,options).catch(()=>{try{new Notification(title||assistantDisplayName(),_notificationOptions(body,options));}catch(_err){}});
  }else if(Notification.permission==='denied'){
    // Explicit "Send test" (force) deserves feedback instead of a silent no-op.
    if(force&&typeof showToast==='function') showToast(t('notifications_denied'),3500,'error');
  }else{
    requestNotificationPermission().then(p=>{if(p==='granted') _showPwaNotification(title,body,options).catch(()=>{try{new Notification(title||assistantDisplayName(),_notificationOptions(body,options));}catch(_err){}});});
  }
}

// ── /btw ephemeral stream ────────────────────────────────────────────────────
// Connects to the ephemeral SSE stream from /api/btw and renders the answer
// in a visually distinct bubble that is NOT persisted to session history.

function attachBtwStream(parentSid, streamId, question){
  if(!parentSid||!streamId) return;
  const src=new EventSource(new URL('api/chat/stream?stream_id='+encodeURIComponent(streamId), document.baseURI||location.href).href);
  let answer='';
  let btwRow=null;
  let _streamDone=false;
  function _ensureBtwRow(){
    if(btwRow&&btwRow.isConnected) return;
    const inner=$('msgInner');
    if(!inner) return;
    btwRow=document.createElement('div');
    btwRow.className='msg-row msg-row-btw';
    btwRow.dataset.role='assistant';
    btwRow.dataset.btw='1';
    const labelEl=document.createElement('div');
    labelEl.className='msg-btw-label';
    labelEl.textContent=t('btw_label');
    const qEl=document.createElement('div');
    qEl.className='msg-body';
    qEl.textContent=question;
    const ansEl=document.createElement('div');
    ansEl.className='msg-body msg-btw-answer';
    ansEl.textContent='...';
    btwRow.appendChild(labelEl);
    btwRow.appendChild(qEl);
    btwRow.appendChild(ansEl);
    inner.appendChild(btwRow);
    btwRow.scrollIntoView({behavior:'smooth',block:'end'});
  }
  src.addEventListener('token',e=>{
    try{answer+=JSON.parse(e.data).text||'';}catch(_){}
    _ensureBtwRow();
    const ansEl=btwRow&&btwRow.querySelector('.msg-btw-answer');
    if(ansEl) ansEl.innerHTML=renderMd(answer);
  });
  src.addEventListener('done',e=>{
    _streamDone=true;
    src.close();
    try{
      const d=JSON.parse(e.data);
      if(d.answer&&!answer) answer=d.answer;
    }catch(_){}
    if(S.session&&S.session.session_id===parentSid) _ensureBtwRow();
    if(btwRow&&btwRow.isConnected){
      const ansEl=btwRow.querySelector('.msg-btw-answer');
      if(ansEl) ansEl.innerHTML=renderMd(answer||t('btw_no_answer'));
    }
    showToast(t('btw_done'));
  });
  src.addEventListener('apperror',e=>{
    _streamDone=true;
    src.close();
    try{
      const d=JSON.parse(e.data);
      showToast(t('btw_failed')+(d.message||''));
    }catch(_){showToast(t('btw_failed'));}
    if(btwRow&&btwRow.isConnected) btwRow.remove();
  });
  src.addEventListener('stream_end',()=>{_streamDone=true;src.close();});
  src.onerror=()=>{src.close();if(!_streamDone&&btwRow&&btwRow.isConnected) btwRow.remove();};
}

// ── /background task tracking ────────────────────────────────────────────────

let _bgPollTimers={};
let _bgActiveTasks=new Set();

function showBackgroundBadge(taskId){
  _bgActiveTasks.add(taskId);
  const badge=$('bgBadge');
  if(badge){
    badge.textContent=String(_bgActiveTasks.size);
    badge.style.display=_bgActiveTasks.size?'':'none';
  }
}
function hideBackgroundBadge(taskId){
  _bgActiveTasks.delete(taskId);
  const badge=$('bgBadge');
  if(badge){
    badge.textContent=String(_bgActiveTasks.size);
    badge.style.display=_bgActiveTasks.size?'':'none';
  }
}
function startBackgroundPolling(parentSid, taskId, prompt){
  if(_bgPollTimers[taskId]) return;
  async function _poll(){
    try{
      const r=await api('/api/background/status?session_id='+encodeURIComponent(parentSid));
      if(r&&r.results){
        for(const res of r.results){
          if(res.task_id===taskId){
            hideBackgroundBadge(taskId);
            delete _bgPollTimers[taskId];
            const msg={role:'assistant',content:`**${t('bg_label')}** ${prompt.slice(0,80)}\n\n${res.answer||t('bg_no_answer')}`,'_background':true,_ts:Date.now()/1000};
            S.messages.push(msg);
            renderMessages({preserveScroll:true});
            showToast(t('bg_complete'));
            return;
          }
        }
      }
    }catch(_){}
    _bgPollTimers[taskId]=setTimeout(_poll,3000);
  }
  _poll();
}

// ── Panel navigation (Chat / Tasks / Skills / Memory) ──
