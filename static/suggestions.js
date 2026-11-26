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

  // ui.js deklariert `const S` auf Top-Level eines klassischen Scripts: die
  // Bindung ist global lesbar, liegt aber NICHT als window.S vor. Beides prüfen.
  function _isBusy(){
    try{
      if(typeof S!=='undefined'&&S&&S.busy) return true;
      return !!(window.S&&window.S.busy);
    }catch(_){ return false; }
  }

  function _currentSid(){
    try{ return (typeof S!=='undefined'&&S&&S.session&&S.session.session_id)||null; }catch(_){ return null; }
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
    const draftFiles=Array.isArray((typeof S!=='undefined'&&S&&S.pendingFiles))?S.pendingFiles.slice():[];
    // Der Chat, dem der Draft gehört — send() und das Warten auf /api/chat/start
    // können dauern, und der Nutzer kann in der Zwischenzeit den Chat wechseln.
    const ownerSid=_currentSid();
    // Die gestagten Anhänge gehören zum Entwurf, NICHT zur Vorschlags-Antwort:
    // send() lädt S.pendingFiles hoch und leert den Tray. Blieben sie während
    // des Klicks gestagt, gingen die Dateien des Entwurfs mit dem Vorschlag raus
    // und fehlten anschließend im wiederhergestellten Entwurf. Also vor dem
    // Senden abkoppeln und zusammen mit dem Text zurücklegen.
    let parkedFiles=null;
    if(draftFiles.length&&typeof S!=='undefined'&&S&&Array.isArray(S.pendingFiles)){
      parkedFiles=draftFiles;
      S.pendingFiles=[];
      if(typeof window.renderTray==='function'){ try{ window.renderTray(); }catch(_){} }
    }
    msg.value=label;
    try{
      const fn=window.send;
      if(typeof fn==='function') await fn();
    }finally{
      const _sameChat=_currentSid()===ownerSid;
      const _untouched=!msg.value.trim()||msg.value===label;
      // Die geparkten Anhänge gehören dem Entwurf ihres Chats, nicht der gerade
      // offenen Ansicht: solange derselbe Chat offen ist, kommen sie in den Tray
      // zurück — auch wenn der Nutzer während des Sendens weitergetippt hat.
      // Sonst hätte der Tipp-Autosave den inzwischen geleerten Tray bereits
      // persistiert und die Dateien wären weg.
      if(parkedFiles&&_sameChat&&typeof S!=='undefined'&&S&&Array.isArray(S.pendingFiles)){
        const _now=S.pendingFiles.slice();
        for(let i=0;i<parkedFiles.length;i++){
          if(_now.indexOf(parkedFiles[i])<0) _now.push(parkedFiles[i]);
        }
        S.pendingFiles=_now;
        if(typeof window.renderTray==='function'){ try{ window.renderTray(); }catch(_){} }
      }
      // Text nur zurücklegen, wenn niemand getippt hat und wir im selben Chat
      // sind: ein neuerer Entwurf wird nie überschrieben.
      if(_sameChat&&_untouched){
        msg.value=draft;
        if(typeof window.autoResize==='function'){
          try{ window.autoResize(); }catch(_){}
        }
      }
      // send() hat den Server-Draft dieses Chats geleert (text:""). Neu hinterlegen:
      //  - mit geparkten Anhängen immer (der Tipp-Autosave bzw. send() hat sie
      //    sonst schon aus dem gespeicherten Entwurf entfernt); im selben Chat
      //    mit neuer Eingabe ist der aktuelle Feldwert der Entwurf, sonst der
      //    ursprüngliche Text — auch bei reinen Anhang-Entwürfen ohne Text;
      //  - ohne Anhänge wie bisher nur den erfassten Text (gleicher Chat und
      //    unangetastet, oder nach einem Chatwechsel).
      const _saveText=(_sameChat&&!_untouched)?msg.value:draft;
      const _persistDraft=!!(parkedFiles&&parkedFiles.length)||(!!draft&&(_untouched||!_sameChat));
      if(_persistDraft&&ownerSid&&typeof _saveComposerDraft==='function'){
        try{ _saveComposerDraft(ownerSid,_saveText,draftFiles); }catch(_){}
      }
    }
  }

  function _rootEl(){
    return document.getElementById('msgInner')||document.getElementById('messages');
  }

  // Die Nachricht, zu der ein Knoten gehört: das direkte Kind des Transcript-
  // Roots, in dem er steckt. Bewusst ohne Klassennamen — die Struktur bleibt
  // gleich, auch wenn Zeilen anders benannt werden.
  function _ownerRow(node){
    const root=_rootEl();
    if(!root||!node||!node.parentElement) return null;
    let el=node;
    while(el.parentElement&&el.parentElement!==root) el=el.parentElement;
    return el.parentElement===root?el:null;
  }

  function _lastMessageRow(){
    const root=_rootEl();
    if(!root||!root.children) return null;
    const kids=root.children;
    for(let i=kids.length-1;i>=0;i--){
      const kid=kids[i];
      if(kid.querySelector&&kid.querySelector('.msg-body')) return kid;
    }
    return null;
  }

  // Nur die letzte Nachricht im Verlauf behält klickbare Vorschläge. Sobald eine
  // spätere User-Nachricht existiert (getippt, nicht geklickt), ist die Frage
  // von vorhin beantwortet — ein Klick würde eine alte Frage erneut senden.
  function _isLiveGroup(group){
    const row=_ownerRow(group);
    if(!row) return false;
    return row===_lastMessageRow();
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
    const live=_isLiveGroup(group);
    group.classList.toggle('inline-suggestions-stale',!live&&!sent);
    _applyGroupDisabled(group,sent||_isBusy()||!live);
  }

  function _syncAllGroups(){
    document.querySelectorAll('.inline-suggestions').forEach(_syncGroupState);
  }

  function _handleSuggestionClick(btn,group,label){
    if(btn.disabled||group.dataset.suggestionsSent==='1') return;
    if(_isBusy()||!_isLiveGroup(group)) return;
    // Mark sent BEFORE the async send so a second rapid click (or the
    // Ctrl/Cmd+Enter shortcut firing in the same tick) can never double-send.
    group.dataset.suggestionsSent='1';
    _applyGroupDisabled(group,true);
    _sendSuggestionLabel(label).catch(()=>{});
  }

  // Nutzer-sichtbarer Name der Gruppe: über die Locale (en trägt den Text, alle
  // anderen Sprachen fallen darauf zurück) statt fest im Markup.
  function _groupAriaLabel(){
    let label='';
    try{ if(typeof t==='function') label=t('suggestions_group_label'); }catch(_){ label=''; }
    if(!label||label==='suggestions_group_label') label='Suggested replies';
    return label;
  }

  function _buildGroup(items){
    const group=document.createElement('div');
    group.className='inline-suggestions';
    group.setAttribute('role','group');
    group.setAttribute('aria-label',_groupAriaLabel());
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
    // Nur Antworten bekommen Buttons: ein Beispiel-Fence in einer User-Nachricht
    // ist Inhalt, kein Angebot. Zeilen ohne data-role gelten als Antwort.
    const roleBody=msgBody.closest('[data-role]');
    const role=roleBody?roleBody.getAttribute('data-role'):null;
    if(role&&role!=='assistant'){
      if(code._suggestionsGroup){ code._suggestionsGroup.remove(); code._suggestionsGroup=null; }
      code.classList.remove('suggestions-validated');
      _unhideRawBlock(pre);
      return;
    }
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

  // Nur die Gruppe an der letzten Nachricht darf die Tastenkombination bedienen —
  // sonst antwortet ein Tastendruck auf eine Frage von vor drei Zügen.
  function _liveGroup(){
    const root=_rootEl();
    if(!root) return null;
    const last=_lastMessageRow();
    const groups=root.querySelectorAll('.inline-suggestions');
    for(let i=groups.length-1;i>=0;i--){
      if(_ownerRow(groups[i])===last) return groups[i];
    }
    return null;
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
    const group=_liveGroup();
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
