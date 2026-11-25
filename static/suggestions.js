// Inline follow-up suggestion buttons (SPEC-inline-suggestions.md).
//
// The model appends ONE fenced ```suggestions``` block at the end of an
// assistant message: a JSON array of 1..6 non-empty strings (<=200 chars
// each). This script finds that block wherever it lands in the DOM — a
// live streaming turn (static/vendor/smd.min.js renders the fence as
// <pre><code class="suggestions">), or a restored/history message (ui.js
// renderMd() renders it as <div class="pre-header">suggestions</div>
// <pre><code class="language-suggestions">) — hides the raw block, and
// renders real buttons directly after the message's .msg-body.
//
// Deliberately does not patch the streaming markdown renderer: a debounced
// MutationObserver over the chat container plus an initial scan covers both
// the live-stream and history-restore cases without touching smd/renderMd.
(function(){
  'use strict';

  const MAX_ITEMS=6;
  const MAX_LEN=200;
  const SCAN_DEBOUNCE_MS=80;
  const BUSY_SYNC_MS=150;

  function _isMac(){
    try{
      const s=(navigator.userAgentData&&navigator.userAgentData.platform)||navigator.platform||navigator.userAgent||'';
      return /Mac|iPhone|iPod|iPad/i.test(s);
    }catch(_){ return false; }
  }

  function _isBusy(){
    try{ return !!(window.S&&window.S.busy); }catch(_){ return false; }
  }

  // Strict per SPEC: JSON array of 1..6 non-empty strings, each <=200 chars.
  // Any other shape (bad JSON, wrong type, empty/oversized array or item)
  // fails safe — returns null, which leaves the raw block visible and
  // renders no buttons (requirement #6).
  function _parseSuggestionsJson(raw){
    let data;
    try{ data=JSON.parse(raw); }catch(_){ return null; }
    if(!Array.isArray(data)||data.length<1||data.length>MAX_ITEMS) return null;
    const out=[];
    for(let i=0;i<data.length;i++){
      const item=data[i];
      if(typeof item!=='string') return null;
      if(!item.trim()||item.length>MAX_LEN) return null;
      out.push(item);
    }
    return out;
  }

  function _hideRawBlock(pre){
    if(!pre||!pre.classList) return;
    pre.classList.add('suggestions-block-hidden');
    const prev=pre.previousElementSibling;
    if(prev&&prev.classList&&prev.classList.contains('pre-header')){
      prev.classList.add('suggestions-block-hidden');
    }
  }

  function _unhideRawBlock(pre){
    if(!pre||!pre.classList) return;
    pre.classList.remove('suggestions-block-hidden');
    const prev=pre.previousElementSibling;
    if(prev&&prev.classList&&prev.classList.contains('pre-header')){
      prev.classList.remove('suggestions-block-hidden');
    }
  }

  async function _sendSuggestionLabel(label){
    const msg=document.getElementById('msg');
    if(!msg) return;
    const draft=msg.value;
    msg.value=label;
    try{
      const fn=window.send;
      if(typeof fn==='function') await fn();
    }finally{
      // Restore the draft only while the composer still holds our own label (or is
      // already empty). send() clears it at some later point, and the user may
      // already be typing the next message by the time this resolves — never
      // clobber that.
      const _untouched=!msg.value.trim()||msg.value===label;
      if(draft&&_untouched){
        msg.value=draft;
        if(typeof window.autoResize==='function'){
          try{ window.autoResize(); }catch(_){}
        }
      }
    }
  }

  function _applyGroupDisabled(group,disabled){
    const buttons=group.querySelectorAll('.inline-suggestion-btn');
    for(let i=0;i<buttons.length;i++){
      const btn=buttons[i];
      btn.disabled=!!disabled;
      btn.setAttribute('aria-disabled',disabled?'true':'false');
    }
  }

  function _syncGroupState(group){
    if(!group||!group.isConnected) return;
    const sent=group.dataset.suggestionsSent==='1';
    _applyGroupDisabled(group,sent||_isBusy());
  }

  function _syncAllGroups(){
    document.querySelectorAll('.inline-suggestions').forEach(_syncGroupState);
  }

  function _handleSuggestionClick(btn,group,label){
    if(btn.disabled||group.dataset.suggestionsSent==='1') return;
    if(_isBusy()) return;
    // Mark sent BEFORE the async send so a second rapid click (or the
    // Ctrl/Cmd+Enter shortcut firing in the same tick) can never double-send.
    group.dataset.suggestionsSent='1';
    _applyGroupDisabled(group,true);
    _sendSuggestionLabel(label).catch(()=>{});
  }

  function _buildGroup(items){
    const group=document.createElement('div');
    group.className='inline-suggestions';
    group.setAttribute('role','group');
    group.setAttribute('aria-label','Suggested replies');
    const mac=_isMac();
    items.forEach((label,i)=>{
      const btn=document.createElement('button');
      btn.type='button';
      btn.className='inline-suggestion-btn '+(i===0?'primary':'secondary');
      btn.setAttribute('aria-disabled','false');
      btn.textContent=label;
      if(i===0){
        const kbd=document.createElement('span');
        kbd.className='inline-suggestion-kbd';
        kbd.setAttribute('aria-hidden','true');
        kbd.textContent=mac?'⌘↵':'Ctrl↵';
        btn.appendChild(kbd);
      }
      btn.addEventListener('click',()=>_handleSuggestionClick(btn,group,label));
      group.appendChild(btn);
    });
    return group;
  }

  // Processes one `<code class="suggestions">` / `<code class="language-suggestions">`
  // element: validates its text, and (re)builds/removes the button group and
  // raw-block hiding to match. Idempotent — a scan that finds the exact same
  // already-rendered text on an already-connected group does nothing.
  function _processCode(code){
    const pre=code.parentElement;
    if(!pre||pre.tagName!=='PRE') return;
    const msgBody=code.closest('.msg-body');
    if(!msgBody) return;
    const raw=code.textContent;
    if(code._suggestionsRaw===raw&&code._suggestionsGroup&&code._suggestionsGroup.isConnected){
      return;
    }
    const items=_parseSuggestionsJson(raw);
    code._suggestionsRaw=raw;
    if(!items){
      // Fail safe: never hide an invalid block, never render buttons for it.
      if(code._suggestionsGroup){ code._suggestionsGroup.remove(); code._suggestionsGroup=null; }
      code.classList.remove('suggestions-validated');
      _unhideRawBlock(pre);
      return;
    }
    code.classList.add('suggestions-validated');
    _hideRawBlock(pre);
    if(code._suggestionsGroup) code._suggestionsGroup.remove();
    const group=_buildGroup(items);
    group._sourceCode=code;
    code._suggestionsGroup=group;
    msgBody.parentNode.insertBefore(group,msgBody.nextSibling);
    _syncGroupState(group);
  }

  // Removes button groups whose source block is gone or was replaced (e.g. an
  // in-place msg-body.innerHTML rebuild after an edit) — requirement #5.
  function _cleanupStaleGroups(root){
    root.querySelectorAll('.inline-suggestions').forEach(group=>{
      const code=group._sourceCode;
      if(!code||!code.isConnected||code._suggestionsGroup!==group){
        group.remove();
      }
    });
  }

  function _scanAll(){
    const root=document.getElementById('msgInner')||document.getElementById('messages');
    if(!root) return;
    _cleanupStaleGroups(root);
    const codes=root.querySelectorAll('pre > code.suggestions, pre > code.language-suggestions');
    codes.forEach(_processCode);
  }

  let _scanTimer=null;
  function _scheduleScan(){
    if(_scanTimer) return;
    _scanTimer=setTimeout(()=>{ _scanTimer=null; _scanAll(); },SCAN_DEBOUNCE_MS);
  }

  function _newestGroup(){
    const groups=document.querySelectorAll('.inline-suggestions');
    return groups.length?groups[groups.length-1]:null;
  }

  function _isImeEnter(e){
    return !!(e.isComposing||e.keyCode===229);
  }

  function _handleComposerKeydown(e){
    if(!e||e.key!=='Enter') return;
    if(!(e.ctrlKey||e.metaKey)) return;
    if(_isImeEnter(e)) return;
    const msg=document.getElementById('msg');
    if(!msg||msg.value.trim()) return;
    const group=_newestGroup();
    if(!group) return;
    const primary=group.querySelector('.inline-suggestion-btn.primary');
    if(!primary||primary.disabled) return;
    e.preventDefault();
    // boot.js's own #msg keydown listener (registered after this one, so it
    // would fire next) also treats a bare Ctrl/Cmd+Enter as "send" under the
    // default/ctrl+enter send-key settings. Left alone it would call send()
    // a second time with the label this click is about to put in the
    // composer. Stop it from running at all for this keydown.
    if(typeof e.stopImmediatePropagation==='function') e.stopImmediatePropagation();
    primary.click();
  }

  function _init(){
    const root=document.getElementById('messages');
    const msg=document.getElementById('msg');
    if(!root||!msg) return false;
    const cfg=window.__HERMES_CONFIG__;
    if(cfg&&cfg.suggestionsEnabled===false) return true; // switched off process-wide
    _scanAll();
    const observer=new MutationObserver(_scheduleScan);
    observer.observe(root,{childList:true,subtree:true,characterData:true});
    msg.addEventListener('keydown',_handleComposerKeydown);
    setInterval(_syncAllGroups,BUSY_SYNC_MS);
    return true;
  }

  if(!_init()){
    document.addEventListener('DOMContentLoaded',_init,{once:true});
  }
})();
