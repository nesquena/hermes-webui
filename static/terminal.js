const TERMINAL_UI={
  open:false,
  collapsed:false,
  sessionId:null,
  workspace:null,
  source:null,
  term:null,
  fitAddon:null,
  resizeObserver:null,
  resizeTimer:null,
  closeTimer:null,
  typedLine:'',
  height:null,
  resizeHandleReady:false,
  resizing:false,
  resizeStartY:0,
  resizeStartHeight:0,
};

const TERMINAL_HEIGHT_DEFAULT=260;
const TERMINAL_HEIGHT_MIN=180;
const TERMINAL_HEIGHT_MAX=520;
const TERMINAL_MOBILE_HEIGHT_DEFAULT=190;
const TERMINAL_MOBILE_HEIGHT_MIN=140;
const TERMINAL_MOBILE_HEIGHT_MAX=300;

function _terminalEls(){
  return {
    panel:$('composerTerminalPanel'),
    inner:$('composerTerminalPanel')&&$('composerTerminalPanel').querySelector('.composer-terminal-inner'),
    dock:$('composerTerminalDock'),
    handle:$('terminalResizeHandle'),
    viewport:$('terminalViewport'),
    surface:$('terminalSurface'),
    toggle:$('btnTerminalToggle'),
    workspace:$('terminalWorkspaceLabel'),
    dockWorkspace:$('terminalDockWorkspaceLabel'),
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

function _terminalHeightBounds(){
  const mobile=window.matchMedia&&window.matchMedia('(max-width: 700px)').matches;
  const min=mobile?TERMINAL_MOBILE_HEIGHT_MIN:TERMINAL_HEIGHT_MIN;
  const maxByViewport=Math.floor(window.innerHeight*(mobile?0.44:0.5));
  const hardMax=mobile?TERMINAL_MOBILE_HEIGHT_MAX:TERMINAL_HEIGHT_MAX;
  return {
    min,
    max:Math.max(min,Math.min(hardMax,maxByViewport)),
    defaultHeight:mobile?TERMINAL_MOBILE_HEIGHT_DEFAULT:TERMINAL_HEIGHT_DEFAULT,
  };
}

function _clampTerminalHeight(height){
  const bounds=_terminalHeightBounds();
  const n=Number(height);
  const fallback=TERMINAL_UI.height||bounds.defaultHeight;
  return Math.max(bounds.min,Math.min(bounds.max,Number.isFinite(n)?n:fallback));
}

function _applyTerminalHeight(height){
  const {inner,handle}= _terminalEls();
  const next=_clampTerminalHeight(height);
  TERMINAL_UI.height=next;
  if(inner)inner.style.setProperty('--composer-terminal-height',next+'px');
  if(handle){
    const bounds=_terminalHeightBounds();
    handle.setAttribute('aria-valuemin',String(bounds.min));
    handle.setAttribute('aria-valuemax',String(bounds.max));
    handle.setAttribute('aria-valuenow',String(next));
  }
  if(TERMINAL_UI.open&&!TERMINAL_UI.collapsed){
    _fitTerminal();
    _syncTerminalTranscriptSpace(true);
  }
  return next;
}

function _resetTerminalHeightForViewport(){
  const bounds=_terminalHeightBounds();
  _applyTerminalHeight(TERMINAL_UI.height||bounds.defaultHeight);
}

function _startTerminalHeightResize(ev){
  if(ev.pointerType==='touch')return;
  const {inner,handle}= _terminalEls();
  if(!inner||!handle)return;
  ev.preventDefault();
  TERMINAL_UI.resizing=true;
  TERMINAL_UI.resizeStartY=ev.clientY;
  TERMINAL_UI.resizeStartHeight=TERMINAL_UI.height||inner.getBoundingClientRect().height||_terminalHeightBounds().defaultHeight;
  inner.classList.add('is-resizing');
  try{handle.setPointerCapture(ev.pointerId);}catch(_){}
}

function _moveTerminalHeightResize(ev){
  if(!TERMINAL_UI.resizing)return;
  ev.preventDefault();
  _applyTerminalHeight(TERMINAL_UI.resizeStartHeight+(TERMINAL_UI.resizeStartY-ev.clientY));
}

function _endTerminalHeightResize(ev){
  if(!TERMINAL_UI.resizing)return;
  TERMINAL_UI.resizing=false;
  const {inner,handle}= _terminalEls();
  if(inner)inner.classList.remove('is-resizing');
  if(handle&&ev&&ev.pointerId!==undefined)try{handle.releasePointerCapture(ev.pointerId);}catch(_){}
  _fitTerminal();
}

function _handleTerminalResizeKey(ev){
  let delta=0;
  if(ev.key==='ArrowUp')delta=16;
  else if(ev.key==='ArrowDown')delta=-16;
  else if(ev.key==='PageUp')delta=64;
  else if(ev.key==='PageDown')delta=-64;
  else if(ev.key==='Home'){
    ev.preventDefault();
    return _applyTerminalHeight(_terminalHeightBounds().min);
  }
  else if(ev.key==='End'){
    ev.preventDefault();
    return _applyTerminalHeight(_terminalHeightBounds().max);
  }
  else return;
  ev.preventDefault();
  _applyTerminalHeight((TERMINAL_UI.height||_terminalHeightBounds().defaultHeight)+delta);
}

function _initTerminalResizeHandle(){
  if(TERMINAL_UI.resizeHandleReady)return;
  const {handle}= _terminalEls();
  if(!handle)return;
  TERMINAL_UI.resizeHandleReady=true;
  handle.addEventListener('pointerdown',_startTerminalHeightResize);
  handle.addEventListener('pointermove',_moveTerminalHeightResize);
  handle.addEventListener('pointerup',_endTerminalHeightResize);
  handle.addEventListener('pointercancel',_endTerminalHeightResize);
  handle.addEventListener('keydown',_handleTerminalResizeKey);
}

function _terminalMessagesEl(){
  return document.getElementById('messages');
}

function _terminalIsMessagesNearBottom(el){
  if(!el)return false;
  return el.scrollHeight-el.scrollTop-el.clientHeight<150;
}

function _syncTerminalTranscriptSpace(open,opts){
  opts=opts||{};
  const messages=_terminalMessagesEl();
  if(!messages)return;
  const wasNearBottom=_terminalIsMessagesNearBottom(messages);
  if(!open){
    messages.classList.remove('terminal-open');
    messages.classList.remove('terminal-collapsed');
    messages.style.removeProperty('--terminal-card-height');
    messages.style.removeProperty('--terminal-dock-height');
    if(wasNearBottom&&typeof scrollToBottom==='function')requestAnimationFrame(scrollToBottom);
    return;
  }
  if(open==='collapsed'){
    messages.classList.remove('terminal-open');
    messages.classList.add('terminal-collapsed');
  }else{
    messages.classList.add('terminal-open');
    messages.classList.remove('terminal-collapsed');
  }
  const measure=()=>{
    if(!TERMINAL_UI.open)return;
    const {panel,inner,dock}= _terminalEls();
    const target=open==='collapsed'?(dock||panel):(inner||panel);
    const h=target&&target.getBoundingClientRect().height;
    if(h>0){
      if(open==='collapsed')messages.style.setProperty('--terminal-dock-height',Math.ceil(h+24)+'px');
      else messages.style.setProperty('--terminal-card-height',Math.ceil(h+24)+'px');
    }
    if(wasNearBottom&&typeof scrollToBottom==='function')scrollToBottom();
  };
  if(opts.immediate)measure();
  requestAnimationFrame(measure);
  setTimeout(measure,420);
}

function _fitTerminal(){
  const term=TERMINAL_UI.term;
  if(!term)return;
  if(TERMINAL_UI.collapsed)return;
  try{
    if(TERMINAL_UI.fitAddon)TERMINAL_UI.fitAddon.fit();
  }catch(_){}
  _syncTerminalTranscriptSpace(true);
  _scheduleTerminalResize();
}

function _setTerminalChromeState(state){
  const {panel,inner,dock,workspace,dockWorkspace}= _terminalEls();
  const composerWrap=$('composerWrap');
  if(!panel)return;
  const collapsed=state==='collapsed';
  const expanded=state==='expanded';
  if(composerWrap)composerWrap.classList.toggle('terminal-dock-visible',collapsed);
  panel.hidden=!(collapsed||expanded);
  panel.classList.toggle('is-open',expanded);
  panel.classList.toggle('is-collapsed',collapsed);
  if(inner)inner.setAttribute('aria-hidden',collapsed?'true':'false');
  if(dock)dock.hidden=!collapsed;
  const label=_terminalWorkspaceName();
  if(workspace)workspace.textContent=label;
  if(dockWorkspace)dockWorkspace.textContent=label;
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
  toggle.title=hasWorkspace?(TERMINAL_UI.collapsed?t('terminal_expand'):t('terminal_open_title')):t('terminal_no_workspace_title');
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
    if(TERMINAL_UI.open){
      if(TERMINAL_UI.collapsed)expandComposerTerminal();
      else focusComposerTerminalInput();
      return;
    }
    const {panel,inner}= _terminalEls();
    if(!panel)return;
    clearTimeout(TERMINAL_UI.closeTimer);
    _initTerminalResizeHandle();
    _resetTerminalHeightForViewport();
    _setTerminalChromeState('expanded');
    requestAnimationFrame(()=>{
      panel.classList.add('is-open');
      window.setTimeout(_fitTerminal,80);
    });
    TERMINAL_UI.open=true;
    TERMINAL_UI.collapsed=false;
    _syncTerminalTranscriptSpace(true);
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

function collapseComposerTerminal(){
  if(!TERMINAL_UI.open||TERMINAL_UI.collapsed)return;
  TERMINAL_UI.collapsed=true;
  _setTerminalChromeState('collapsed');
  _syncTerminalTranscriptSpace('collapsed');
  syncTerminalButton();
}

function expandComposerTerminal(){
  if(!TERMINAL_UI.open)return;
  TERMINAL_UI.collapsed=false;
  clearTimeout(TERMINAL_UI.closeTimer);
  _syncTerminalTranscriptSpace(true,{immediate:true});
  _setTerminalChromeState('expanded');
  _resetTerminalHeightForViewport();
  requestAnimationFrame(()=>{
    _fitTerminal();
    focusComposerTerminalInput();
  });
  syncTerminalButton();
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
    panel.classList.remove('is-open','is-collapsed');
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
  TERMINAL_UI.collapsed=false;
  const composerWrap=$('composerWrap');
  if(composerWrap)composerWrap.classList.remove('terminal-dock-visible');
  TERMINAL_UI.sessionId=null;
  TERMINAL_UI.workspace=null;
  syncTerminalButton();
}

async function restartComposerTerminal(){
  if(!TERMINAL_UI.open||TERMINAL_UI.collapsed)return;
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
  if(!TERMINAL_UI.open||TERMINAL_UI.collapsed)return;
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

window.addEventListener('resize',()=>{
  if(!TERMINAL_UI.open)return;
  if(TERMINAL_UI.collapsed){
    _syncTerminalTranscriptSpace('collapsed');
    return;
  }
  _resetTerminalHeightForViewport();
});

if(window.MutationObserver){
  new MutationObserver(syncComposerTerminalTheme).observe(document.documentElement,{
    attributes:true,
    attributeFilter:['class','data-skin'],
  });
}
