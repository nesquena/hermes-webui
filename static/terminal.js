const TERMINAL_UI={
  open:false,
  sessionId:null,
  workspace:null,
  source:null,
  term:null,
  fitAddon:null,
  resizeObserver:null,
  resizeTimer:null,
  closeTimer:null,
  typedLine:'',
};

function _terminalEls(){
  return {
    panel:$('composerTerminalPanel'),
    inner:$('composerTerminalPanel')&&$('composerTerminalPanel').querySelector('.composer-terminal-inner'),
    viewport:$('terminalViewport'),
    surface:$('terminalSurface'),
    toggle:$('btnTerminalToggle'),
    workspace:$('terminalWorkspaceLabel'),
  };
}

function _terminalSessionId(){
  return S.session&&S.session.session_id;
}

function _terminalWorkspaceName(){
  const ws=S.session&&S.session.workspace;
  if(!ws)return '';
  const parts=String(ws).split(/[\\/]+/).filter(Boolean);
  return parts[parts.length-1]||ws;
}

function _isTerminalCloseCommand(value){
  return ['exit','quit','logout','close'].includes(String(value||'').trim().toLowerCase());
}

function _trackTerminalInput(data){
  if(data==='\r'||data==='\n'){
    const command=TERMINAL_UI.typedLine;
    TERMINAL_UI.typedLine='';
    return command;
  }
  if(data==='\u0003'){
    TERMINAL_UI.typedLine='';
    return null;
  }
  if(data==='\u007f'||data==='\b'){
    TERMINAL_UI.typedLine=TERMINAL_UI.typedLine.slice(0,-1);
    return null;
  }
  if(data.length===1&&data>=' '){
    TERMINAL_UI.typedLine+=data;
  }else if(data.length>1&&/^[\x20-\x7e]+$/.test(data)){
    TERMINAL_UI.typedLine+=data;
  }
  return null;
}

function _terminalCssVar(name,fallback){
  const value=getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value||fallback;
}

function _terminalTheme(){
  const isDark=document.documentElement.classList.contains('dark');
  const background=_terminalCssVar('--code-bg',isDark?'#1A1A2E':'#F5F0E5');
  const foreground=_terminalCssVar('--pre-text',_terminalCssVar('--text',isDark?'#E2E8F0':'#1A1610'));
  const muted=_terminalCssVar('--muted',isDark?'#C0C0C0':'#5C5344');
  const accent=_terminalCssVar('--accent-text',_terminalCssVar('--accent',isDark?'#FFD700':'#8B6508'));
  const error=_terminalCssVar('--error',isDark?'#EF5350':'#C62828');
  const success=_terminalCssVar('--success',isDark?'#4CAF50':'#3D8B40');
  const warning=_terminalCssVar('--warning',isDark?'#FFA726':'#E68A00');
  const info=_terminalCssVar('--info',isDark?'#4DD0E1':'#0288A8');
  return {
    background,
    foreground,
    cursor:accent,
    selectionBackground:_terminalCssVar('--accent-bg-strong',isDark?'rgba(255,215,0,.18)':'rgba(184,134,11,.18)'),
    black:isDark?'#0D0D1A':'#1A1610',
    red:error,
    green:success,
    yellow:warning,
    blue:info,
    magenta:accent,
    cyan:info,
    white:foreground,
    brightBlack:muted,
    brightRed:error,
    brightGreen:success,
    brightYellow:accent,
    brightBlue:info,
    brightMagenta:accent,
    brightCyan:info,
    brightWhite:isDark?'#FFFFFF':'#0F0D08',
  };
}

function syncComposerTerminalTheme(){
  if(TERMINAL_UI.term)TERMINAL_UI.term.options.theme=_terminalTheme();
}

function _xtermReady(){
  return typeof window.Terminal==='function';
}

function _ensureXterm(){
  const {surface}= _terminalEls();
  if(!surface)return null;
  if(TERMINAL_UI.term)return TERMINAL_UI.term;
  if(!_xtermReady()){
    surface.textContent='Terminal library failed to load. Check network access to cdn.jsdelivr.net.';
    return null;
  }
  const term=new window.Terminal({
    cursorBlink:true,
    fontSize:13,
    fontFamily:'Menlo, Monaco, Consolas, "Liberation Mono", monospace',
    scrollback:1000,
    convertEol:false,
    theme:_terminalTheme(),
  });
  let fitAddon=null;
  if(window.FitAddon&&typeof window.FitAddon.FitAddon==='function'){
    fitAddon=new window.FitAddon.FitAddon();
    term.loadAddon(fitAddon);
  }
  if(window.WebLinksAddon&&typeof window.WebLinksAddon.WebLinksAddon==='function'){
    term.loadAddon(new window.WebLinksAddon.WebLinksAddon());
  }
  term.open(surface);
  term.onData(data=>{
    const completedCommand=_trackTerminalInput(data);
    if(completedCommand!==null&&_isTerminalCloseCommand(completedCommand)){
      closeComposerTerminal();
      return;
    }
    const sid=TERMINAL_UI.sessionId||_terminalSessionId();
    if(!sid)return;
    api('/api/terminal/input',{method:'POST',body:JSON.stringify({
      session_id:sid,
      data,
    })}).catch(e=>showToast(t('terminal_input_failed')+e.message,2600,'error'));
  });
  TERMINAL_UI.term=term;
  TERMINAL_UI.fitAddon=fitAddon;
  _fitTerminal();
  return term;
}

function _terminalDimensions(){
  const term=TERMINAL_UI.term;
  if(term&&term.cols&&term.rows)return {rows:term.rows,cols:term.cols};
  return {rows:18,cols:80};
}

function _terminalMessagesEl(){
  return document.getElementById('messages');
}

function _terminalIsMessagesNearBottom(el){
  if(!el)return false;
  return el.scrollHeight-el.scrollTop-el.clientHeight<150;
}

function _syncTerminalTranscriptSpace(open){
  const messages=_terminalMessagesEl();
  if(!messages)return;
  const wasNearBottom=_terminalIsMessagesNearBottom(messages);
  if(!open){
    messages.classList.remove('terminal-open');
    messages.style.removeProperty('--terminal-card-height');
    if(wasNearBottom&&typeof scrollToBottom==='function')requestAnimationFrame(scrollToBottom);
    return;
  }
  messages.classList.add('terminal-open');
  const measure=()=>{
    if(!TERMINAL_UI.open)return;
    const {panel,inner}= _terminalEls();
    const h=(inner||panel)&&((inner||panel).getBoundingClientRect().height);
    if(h>0)messages.style.setProperty('--terminal-card-height',Math.ceil(h+24)+'px');
    if(wasNearBottom&&typeof scrollToBottom==='function')scrollToBottom();
  };
  requestAnimationFrame(measure);
  setTimeout(measure,420);
}

function _fitTerminal(){
  const term=TERMINAL_UI.term;
  if(!term)return;
  try{
    if(TERMINAL_UI.fitAddon)TERMINAL_UI.fitAddon.fit();
  }catch(_){}
  _syncTerminalTranscriptSpace(true);
  _scheduleTerminalResize();
}

function syncTerminalButton(){
  const {toggle}= _terminalEls();
  const currentSid=_terminalSessionId();
  const currentWorkspace=S.session&&S.session.workspace;
  if(TERMINAL_UI.open&&TERMINAL_UI.sessionId&&(currentSid!==TERMINAL_UI.sessionId||currentWorkspace!==TERMINAL_UI.workspace)){
    closeComposerTerminal(TERMINAL_UI.sessionId);
  }
  if(!toggle)return;
  const hasWorkspace=!!(S.session&&S.session.workspace);
  toggle.disabled=!hasWorkspace;
  toggle.classList.toggle('active',TERMINAL_UI.open);
  toggle.setAttribute('aria-pressed',TERMINAL_UI.open?'true':'false');
  toggle.title=hasWorkspace?t('terminal_open_title'):t('terminal_no_workspace_title');
  toggle.setAttribute('aria-label',toggle.title);
}

function focusComposerTerminalInput(){
  if(TERMINAL_UI.term)TERMINAL_UI.term.focus();
}

function _connectTerminalOutput(){
  const sid=_terminalSessionId();
  if(!sid)return;
  if(TERMINAL_UI.source){
    try{TERMINAL_UI.source.close();}catch(_){}
    TERMINAL_UI.source=null;
  }
  const url=new URL('api/terminal/output',location.href);
  url.searchParams.set('session_id',sid);
  const source=new EventSource(url.href,{withCredentials:true});
  TERMINAL_UI.source=source;
  source.addEventListener('output',ev=>{
    if(TERMINAL_UI.source!==source)return;
    let text='';
    try{text=(JSON.parse(ev.data)||{}).text||'';}
    catch(_){text=ev.data||'';}
    if(TERMINAL_UI.term&&text)TERMINAL_UI.term.write(text);
  });
  source.addEventListener('terminal_closed',()=>{
    if(TERMINAL_UI.source!==source)return;
    if(TERMINAL_UI.term)TERMINAL_UI.term.writeln('\r\n[terminal closed]\r\n');
    try{source.close();}catch(_){}
    TERMINAL_UI.source=null;
    setTimeout(()=>closeComposerTerminal(null,{skipApi:true}),260);
  });
  source.addEventListener('terminal_error',ev=>{
    if(TERMINAL_UI.source!==source)return;
    let msg=t('terminal_error');
    try{msg=(JSON.parse(ev.data)||{}).error||msg;}catch(_){}
    if(TERMINAL_UI.term)TERMINAL_UI.term.writeln('\r\n[terminal error] '+msg+'\r\n');
    try{source.close();}catch(_){}
    TERMINAL_UI.source=null;
  });
}

async function _startComposerTerminal(restart=false){
  const sid=_terminalSessionId();
  if(!sid||!(S.session&&S.session.workspace)){
    showToast(t('terminal_no_workspace_title'),2600,'warning');
    syncTerminalButton();
    return;
  }
  const term=_ensureXterm();
  if(!term)return;
  _fitTerminal();
  const dims=_terminalDimensions();
  await api('/api/terminal/start',{method:'POST',body:JSON.stringify({
    session_id:sid,
    rows:dims.rows,
    cols:dims.cols,
    restart:!!restart,
  })});
  TERMINAL_UI.sessionId=sid;
  TERMINAL_UI.workspace=S.session&&S.session.workspace||null;
  TERMINAL_UI.typedLine='';
  _connectTerminalOutput();
  _resizeComposerTerminal();
}

async function toggleComposerTerminal(force){
  const next=typeof force==='boolean'?force:!TERMINAL_UI.open;
  if(next){
    const {panel,inner,workspace}= _terminalEls();
    if(!panel)return;
    clearTimeout(TERMINAL_UI.closeTimer);
    panel.hidden=false;
    requestAnimationFrame(()=>{
      panel.classList.add('is-open');
      window.setTimeout(_fitTerminal,80);
    });
    TERMINAL_UI.open=true;
    _syncTerminalTranscriptSpace(true);
    if(workspace)workspace.textContent=_terminalWorkspaceName();
    syncTerminalButton();
    if(!TERMINAL_UI.resizeObserver&&window.ResizeObserver){
      TERMINAL_UI.resizeObserver=new ResizeObserver(()=>_fitTerminal());
      TERMINAL_UI.resizeObserver.observe(inner||panel);
    }
    try{
      await _startComposerTerminal(false);
      focusComposerTerminalInput();
    }catch(e){
      showToast(t('terminal_start_failed')+e.message,3200,'error');
    }
  }else{
    await closeComposerTerminal();
  }
}

function _disposeXterm(){
  if(TERMINAL_UI.term){
    try{TERMINAL_UI.term.dispose();}catch(_){}
  }
  TERMINAL_UI.term=null;
  TERMINAL_UI.fitAddon=null;
  TERMINAL_UI.typedLine='';
  const {surface}= _terminalEls();
  if(surface)surface.textContent='';
}

async function closeComposerTerminal(sessionId,opts){
  opts=opts||{};
  const sid=sessionId||TERMINAL_UI.sessionId||_terminalSessionId();
  if(TERMINAL_UI.source){
    try{TERMINAL_UI.source.close();}catch(_){}
    TERMINAL_UI.source=null;
  }
  if(sid&&!opts.skipApi){
    api('/api/terminal/close',{method:'POST',body:JSON.stringify({session_id:sid})}).catch(()=>{});
  }
  const {panel}= _terminalEls();
  if(panel){
    panel.classList.remove('is-open');
    _syncTerminalTranscriptSpace(false);
    clearTimeout(TERMINAL_UI.closeTimer);
    TERMINAL_UI.closeTimer=setTimeout(()=>{
      if(!TERMINAL_UI.open)panel.hidden=true;
      _disposeXterm();
    },280);
  }else{
    _syncTerminalTranscriptSpace(false);
    _disposeXterm();
  }
  TERMINAL_UI.open=false;
  TERMINAL_UI.sessionId=null;
  TERMINAL_UI.workspace=null;
  syncTerminalButton();
}

async function restartComposerTerminal(){
  if(!TERMINAL_UI.open)return;
  if(TERMINAL_UI.source){
    try{TERMINAL_UI.source.close();}catch(_){}
    TERMINAL_UI.source=null;
  }
  if(TERMINAL_UI.term)TERMINAL_UI.term.reset();
  try{await _startComposerTerminal(true);}
  catch(e){showToast(t('terminal_start_failed')+e.message,3200,'error');}
}

function clearComposerTerminal(){
  if(TERMINAL_UI.term)TERMINAL_UI.term.clear();
}

function _terminalBufferText(){
  const term=TERMINAL_UI.term;
  if(!term||!term.buffer)return '';
  const buffer=term.buffer.active;
  const lines=[];
  for(let i=0;i<buffer.length;i++){
    const line=buffer.getLine(i);
    if(line)lines.push(line.translateToString(true));
  }
  return lines.join('\n').trim();
}

async function copyComposerTerminalOutput(){
  try{
    const selection=TERMINAL_UI.term&&TERMINAL_UI.term.getSelection?TERMINAL_UI.term.getSelection():'';
    await navigator.clipboard.writeText(selection||_terminalBufferText());
    showToast(t('copied'));
  }catch(e){
    showToast(t('terminal_copy_failed')+e.message,2600,'error');
  }
}

async function submitComposerTerminalInput(ev){
  if(ev)ev.preventDefault();
}

function _scheduleTerminalResize(){
  clearTimeout(TERMINAL_UI.resizeTimer);
  TERMINAL_UI.resizeTimer=setTimeout(_resizeComposerTerminal,120);
}

async function _resizeComposerTerminal(){
  if(!TERMINAL_UI.open)return;
  const sid=TERMINAL_UI.sessionId||_terminalSessionId();
  if(!sid)return;
  const dims=_terminalDimensions();
  try{
    await api('/api/terminal/resize',{method:'POST',body:JSON.stringify({
      session_id:sid,
      rows:dims.rows,
      cols:dims.cols,
    })});
  }catch(_){}
}

window.addEventListener('beforeunload',()=>{
  if(TERMINAL_UI.source)try{TERMINAL_UI.source.close();}catch(_){}
  if(TERMINAL_UI.sessionId){
    const url=new URL('api/terminal/close',location.href).href;
    const body=JSON.stringify({session_id:TERMINAL_UI.sessionId});
    try{
      navigator.sendBeacon(url,new Blob([body],{type:'application/json'}));
    }catch(_){
      try{fetch(url,{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body,keepalive:true});}catch(__){}
    }
  }
});

if(window.MutationObserver){
  new MutationObserver(syncComposerTerminalTheme).observe(document.documentElement,{
    attributes:true,
    attributeFilter:['class','data-skin'],
  });
}
