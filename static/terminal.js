const TERMINAL_UI={
  open:false,
  collapsed:false,
  mode:'shell',
  sessionId:null,
  workspace:null,
  claudePublicSessionId:null,
  claudeSession:null,
  claudeState:'idle',
  claudeOpenEpoch:0,
  claudeCursor:null,
  handle:null,
  generation:null,
  claudeLabel:null,
  readyPromise:null,
  inputQueue:Promise.resolve(),
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
  lastAppliedTheme:null,
  lastAppliedFontFamily:null,
  fontFitFrame:null,
  fontLoadGeneration:0,
  fontLoadRequest:null,
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
    viewport:$('terminalViewport'),
    surface:$('terminalSurface'),
    toggle:$('btnTerminalToggle'),
    workspace:$('terminalWorkspaceLabel'),
    dockWorkspace:$('terminalDockWorkspaceLabel'),
    handle:$('terminalResizeHandle'),
    title:$('terminalTitleLabel'),
    restart:$('btnTerminalRestart'),
    stopClaude:$('btnTerminalStopClaude'),
    claudeKeys:$('claudeTerminalKeys'),
  };
}

function _terminalSessionId(){
  return S.session&&S.session.session_id;
}

function _terminalWorkspaceName(){
  if(TERMINAL_UI.mode==='claude_code')return TERMINAL_UI.workspace||'';
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

function _terminalMonoFont(){
  return _terminalCssVar(
    '--font-mono',
    'ui-monospace,"SFMono-Regular","SF Mono",Menlo,Consolas,"Liberation Mono",monospace'
  );
}

function _terminalThemesEqual(left,right){
  if(left===right)return true;
  if(!left||!right)return false;
  const leftKeys=Object.keys(left);
  const rightKeys=Object.keys(right);
  return leftKeys.length===rightKeys.length&&leftKeys.every(key=>Object.prototype.hasOwnProperty.call(right,key)&&left[key]===right[key]);
}

function _scheduleTerminalFontFit(term){
  if(TERMINAL_UI.fontFitFrame!==null)return;
  TERMINAL_UI.fontFitFrame=requestAnimationFrame(()=>{
    TERMINAL_UI.fontFitFrame=null;
    if(TERMINAL_UI.term!==term||!TERMINAL_UI.open||TERMINAL_UI.collapsed)return;
    _fitTerminal();
  });
}

function _watchTerminalFontLoad(term,fontFamily){
  const request={
    generation:TERMINAL_UI.fontLoadGeneration+1,
    term,
    fontFamily,
  };
  TERMINAL_UI.fontLoadGeneration=request.generation;
  TERMINAL_UI.fontLoadRequest=request;
  const fontSet=document.fonts;
  if(!fontSet||typeof fontSet.load!=='function')return;
  const fontSize=Number(term.options&&term.options.fontSize);
  const fontShorthand=(Number.isFinite(fontSize)&&fontSize>0?fontSize:13)+'px '+fontFamily;
  let load;
  try{
    load=document.fonts.load(fontShorthand);
  }catch(_){
    return;
  }
  Promise.resolve(load).then(fontFaces=>{
    if(!fontFaces||fontFaces.length===0)return;
    if(TERMINAL_UI.fontLoadRequest!==request
      ||TERMINAL_UI.fontLoadGeneration!==request.generation
      ||TERMINAL_UI.term!==term
      ||!TERMINAL_UI.open
      ||TERMINAL_UI.collapsed
      ||TERMINAL_UI.lastAppliedFontFamily!==fontFamily)return;
    _scheduleTerminalFontFit(term);
  },()=>{});
}

function syncComposerTerminalAppearance(forceFontLoad=false){
  if(!TERMINAL_UI.term)return;
  const term=TERMINAL_UI.term;
  const theme=_terminalTheme();
  const fontFamily=_terminalMonoFont();
  if(!_terminalThemesEqual(TERMINAL_UI.lastAppliedTheme,theme)){
    term.options.theme=theme;
    TERMINAL_UI.lastAppliedTheme={...theme};
  }
  const fontFamilyChanged=TERMINAL_UI.lastAppliedFontFamily!==fontFamily;
  if(fontFamilyChanged){
    term.options.fontFamily=fontFamily;
    TERMINAL_UI.lastAppliedFontFamily=fontFamily;
    _scheduleTerminalFontFit(term);
  }
  if(fontFamilyChanged||forceFontLoad){
    _watchTerminalFontLoad(term,fontFamily);
  }
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
  const theme=_terminalTheme();
  const fontFamily=_terminalMonoFont();
  const term=new window.Terminal({
    cursorBlink:true,
    fontSize:13,
    fontFamily,
    scrollback:1000,
    convertEol:false,
    theme,
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
    if(TERMINAL_UI.mode==='claude_code'){
      _queueClaudeTerminalInput(data);
      return;
    }
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
  TERMINAL_UI.lastAppliedTheme={...theme};
  TERMINAL_UI.lastAppliedFontFamily=fontFamily;
  _fitTerminal();
  _watchTerminalFontLoad(term,fontFamily);
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
    messages.classList.remove('terminal-expanding-from-dock');
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
  const {panel,inner,dock,workspace,dockWorkspace,title,restart,stopClaude,claudeKeys}= _terminalEls();
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
  const claudeMode=TERMINAL_UI.mode==='claude_code';
  const claudeLive=claudeMode&&TERMINAL_UI.claudeState==='live';
  panel.classList.toggle('claude-terminal-mode',claudeMode);
  if(title)title.textContent=claudeMode
    ? (TERMINAL_UI.claudeState==='closed'
      ? t('claude_terminal_closed')
      : (TERMINAL_UI.claudeState==='error'?t('claude_terminal_error'):(TERMINAL_UI.claudeLabel||'Claude')))
    : t('terminal_title');
  if(restart){
    restart.textContent=claudeMode?t('terminal_reconnect'):t('terminal_restart');
    restart.setAttribute('data-i18n',claudeMode?'terminal_reconnect':'terminal_restart');
  }
  if(stopClaude)stopClaude.hidden=!claudeLive;
  if(claudeKeys)claudeKeys.hidden=!claudeLive;
}

function syncTerminalBackendState(data){
  S.terminalRemoteBackend=!!(data&&data.terminal_remote_backend);
  return S.terminalRemoteBackend;
}

function _terminalRemoteBackendUnsupportedMessage(){
  const key=t('terminal_remote_backend_unsupported');
  return key&&key!=='terminal_remote_backend_unsupported'
    ? key
    : 'Embedded terminal is only supported for local terminal backends.';
}

function _terminalStartErrorMessage(err){
  if(err&&err.body){
    try{
      const payload=JSON.parse(err.body);
      if(payload&&payload.error==='remote_terminal_backend_unsupported'){
        S.terminalRemoteBackend=true;
        syncTerminalButton();
        return String(payload.message||_terminalRemoteBackendUnsupportedMessage());
      }
    }catch(_){}
  }
  return err&&err.message?err.message:String(err||'');
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
  const remoteBackend=!!S.terminalRemoteBackend;
  toggle.disabled=!hasWorkspace||remoteBackend;
  toggle.classList.toggle('active',TERMINAL_UI.open);
  toggle.setAttribute('aria-pressed',TERMINAL_UI.open?'true':'false');
  toggle.title=!hasWorkspace
    ? t('terminal_no_workspace_title')
    : (remoteBackend
      ? _terminalRemoteBackendUnsupportedMessage()
      : (TERMINAL_UI.collapsed?t('terminal_expand'):t('terminal_open_title')));
  toggle.setAttribute('aria-label',toggle.title);
}

function focusComposerTerminalInput(){
  if(TERMINAL_UI.term)TERMINAL_UI.term.focus();
}

function _claudeTerminalIdentity(){
  if(TERMINAL_UI.mode!=='claude_code'||!TERMINAL_UI.handle||!TERMINAL_UI.generation)return null;
  return {handle:TERMINAL_UI.handle,generation:TERMINAL_UI.generation};
}

function _claudeTerminalContext(identity){
  return {
    epoch:TERMINAL_UI.claudeOpenEpoch,
    publicSessionId:TERMINAL_UI.claudePublicSessionId,
    handle:identity&&identity.handle||null,
    generation:identity&&identity.generation||null,
  };
}

function _isClaudeTerminalContextCurrent(context,requireIdentity=true){
  if(!context
    ||TERMINAL_UI.mode!=='claude_code'
    ||TERMINAL_UI.claudeOpenEpoch!==context.epoch
    ||TERMINAL_UI.claudePublicSessionId!==context.publicSessionId)return false;
  if(!requireIdentity)return true;
  return TERMINAL_UI.handle===context.handle&&TERMINAL_UI.generation===context.generation;
}

function _canonicalClaudeTerminalCursor(value){
  const cursor=String(value===undefined||value===null?'':value);
  const max='9223372036854775807';
  if(!cursor||cursor.length>max.length)return null;
  if(!/^(0|[1-9]\d*)$/.test(cursor))return null;
  if(cursor.length===max.length&&cursor>max)return null;
  return cursor;
}

function _rememberClaudeTerminalCursor(ev){
  const cursor=_canonicalClaudeTerminalCursor(ev&&ev.lastEventId);
  if(cursor!==null)TERMINAL_UI.claudeCursor=cursor;
}

async function _mintClaudeTerminalToken(operation,identity){
  identity=identity||_claudeTerminalIdentity();
  if(!identity)throw new Error('Claude terminal is not attached');
  await api('/api/claude-code/terminal-token',{method:'POST',body:JSON.stringify({
    handle:identity.handle,
    generation:identity.generation,
    operation,
  })});
}

async function _sendClaudeTerminalInput(data,context){
  if(!_isClaudeTerminalContextCurrent(context,false))return;
  const identity=_claudeTerminalIdentity();
  if(!identity)return;
  context={...context,handle:identity.handle,generation:identity.generation};
  const send=()=>api('/api/claude-code/terminal/input',{method:'POST',retries:0,body:JSON.stringify({
    handle:identity.handle,
    generation:identity.generation,
    data,
  })});
  try{
    await send();
    return _isClaudeTerminalContextCurrent(context);
  }catch(err){
    if(!_isClaudeTerminalContextCurrent(context))return;
    if(!err||err.status!==404)throw err;
    await _mintClaudeTerminalToken('input',identity);
    if(!_isClaudeTerminalContextCurrent(context))return;
    await send();
    return _isClaudeTerminalContextCurrent(context);
  }
}

function _queueClaudeTerminalInput(data){
  const ready=TERMINAL_UI.readyPromise;
  const context=_claudeTerminalContext();
  TERMINAL_UI.inputQueue=TERMINAL_UI.inputQueue
    .then(()=>ready)
    .then(()=>_sendClaudeTerminalInput(data,context))
    .catch(err=>showToast(t('terminal_input_failed')+(err&&err.message?err.message:String(err||'')),2600,'error'));
  return TERMINAL_UI.inputQueue;
}

function sendClaudeTerminalKey(key){
  focusComposerTerminalInput();
  const data={
    esc:'\u001b',
    tab:'\t',
    'ctrl-c':'\u0003',
    up:'\u001b[A',
    down:'\u001b[B',
    right:'\u001b[C',
    left:'\u001b[D',
  }[key];
  if(data)_queueClaudeTerminalInput(data);
}

function _disconnectTerminalSource(){
  if(!TERMINAL_UI.source)return;
  try{if(TERMINAL_UI.source.readyState!==2)TERMINAL_UI.source.close();}catch(_){ }
  TERMINAL_UI.source=null;
}

function _retireClaudeTerminal(state,message){
  if(TERMINAL_UI.mode!=='claude_code')return;
  TERMINAL_UI.claudeOpenEpoch+=1;
  _disconnectTerminalSource();
  TERMINAL_UI.handle=null;
  TERMINAL_UI.generation=null;
  TERMINAL_UI.readyPromise=null;
  TERMINAL_UI.inputQueue=Promise.resolve();
  TERMINAL_UI.claudeCursor=null;
  TERMINAL_UI.claudeState=state;
  if(TERMINAL_UI.term&&message)TERMINAL_UI.term.writeln('\r\n['+message+']\r\n');
  _setTerminalChromeState(TERMINAL_UI.collapsed?'collapsed':'expanded');
}

function _connectClaudeTerminalOutput(context){
  const identity=_claudeTerminalIdentity();
  context=context||_claudeTerminalContext(identity);
  if(!identity||!_isClaudeTerminalContextCurrent(context)||document.hidden)return false;
  _disconnectTerminalSource();
  if(!_isClaudeTerminalContextCurrent(context)||document.hidden)return false;
  const url=new URL('api/claude-code/terminal/output',document.baseURI||location.href);
  url.searchParams.set('handle',identity.handle);
  url.searchParams.set('generation',identity.generation);
  const cursor=_canonicalClaudeTerminalCursor(TERMINAL_UI.claudeCursor);
  if(cursor!==null)url.searchParams.set('cursor',cursor);
  else TERMINAL_UI.claudeCursor=null;
  const source=new EventSource(url.href,{withCredentials:true});
  TERMINAL_UI.source=source;
  source.addEventListener('output',ev=>{
    if(TERMINAL_UI.source!==source)return;
    _rememberClaudeTerminalCursor(ev);
    let text='';
    try{text=(JSON.parse(ev.data)||{}).text||'';}catch(_){text='';}
    if(TERMINAL_UI.term&&text)TERMINAL_UI.term.write(text);
  });
  source.addEventListener('terminal_reset',ev=>{
    if(TERMINAL_UI.source!==source)return;
    _rememberClaudeTerminalCursor(ev);
    let generation='';
    try{generation=(JSON.parse(ev.data)||{}).generation||'';}catch(_){ }
    if(generation&&generation!==TERMINAL_UI.generation)return;
    if(TERMINAL_UI.term)TERMINAL_UI.term.clear();
    try{if(source.readyState!==EventSource.CLOSED)source.close();}catch(_){ }
    if(TERMINAL_UI.source===source)TERMINAL_UI.source=null;
    Promise.resolve().then(()=>{
      if(!_isClaudeTerminalContextCurrent(context)||document.hidden||TERMINAL_UI.source)return false;
      return _reconnectClaudeTerminal();
    }).catch(err=>{
      if(!_isClaudeTerminalContextCurrent(context))return;
      if(typeof recordClientSSEError==='function')recordClientSSEError('claude-terminal',{reason:'Claude terminal reset reconnect failed'});
      if(TERMINAL_UI.term)TERMINAL_UI.term.writeln('\r\n[terminal reconnect failed]\r\n');
    });
  });
  source.addEventListener('terminal_closed',ev=>{
    if(TERMINAL_UI.source!==source)return;
    _rememberClaudeTerminalCursor(ev);
    _retireClaudeTerminal('closed',t('claude_terminal_closed'));
  });
  source.addEventListener('terminal_error',ev=>{
    if(TERMINAL_UI.source!==source)return;
    _rememberClaudeTerminalCursor(ev);
    _retireClaudeTerminal('error',t('claude_terminal_error'));
  });
  source.addEventListener('error',()=>{
    if(TERMINAL_UI.source!==source)return;
    const closed=source.readyState===EventSource.CLOSED;
    if(!source._terminalErrNotified){
      source._terminalErrNotified=true;
      if(typeof recordClientSSEError==='function')recordClientSSEError('claude-terminal',{ready_state:source.readyState,reason:'Claude terminal EventSource.onerror'});
      if(TERMINAL_UI.term)TERMINAL_UI.term.writeln('\r\n[terminal connection lost'+(closed?'':', reconnecting…')+']\r\n');
    }
    if(closed)_disconnectTerminalSource();
  });
  source.addEventListener('open',()=>{
    if(TERMINAL_UI.source===source)source._terminalErrNotified=false;
  });
  return true;
}

async function _reconnectClaudeTerminal(){
  const identity=_claudeTerminalIdentity();
  if(!identity)return;
  const context=_claudeTerminalContext(identity);
  try{
    await _mintClaudeTerminalToken('stream',identity);
  }catch(err){
    if(!_isClaudeTerminalContextCurrent(context))return false;
    if(err&&err.status===404){
      _retireClaudeTerminal('error',t('claude_terminal_error'));
    }
    throw err;
  }
  if(!_isClaudeTerminalContextCurrent(context))return false;
  if(!_connectClaudeTerminalOutput(context))return false;
  await _resizeClaudeTerminal(context);
  return _isClaudeTerminalContextCurrent(context);
}

function _openClaudeTerminalChrome(session){
  if(TERMINAL_UI.open){
    if(TERMINAL_UI.mode==='claude_code')detachClaudeTerminal();
    else{
      closeComposerTerminal();
      clearTimeout(TERMINAL_UI.closeTimer);
      _disposeXterm();
    }
  }
  TERMINAL_UI.claudeOpenEpoch+=1;
  TERMINAL_UI.mode='claude_code';
  TERMINAL_UI.claudePublicSessionId=session.session_id;
  TERMINAL_UI.claudeSession={
    session_id:session.session_id,
    kind:'claude_code',
    profile:session.profile,
    label:session.label,
    workspace_label:session.workspace_label,
    can_remote_resume:true,
  };
  TERMINAL_UI.claudeState='starting';
  TERMINAL_UI.claudeCursor=null;
  TERMINAL_UI.workspace=session.workspace_label||'Claude Code';
  TERMINAL_UI.claudeLabel=session.profile==='qwen'?'Claude Qwen':'Claude Local · Ornith';
  TERMINAL_UI.sessionId=null;
  TERMINAL_UI.handle=null;
  TERMINAL_UI.generation=null;
  TERMINAL_UI.readyPromise=null;
  TERMINAL_UI.inputQueue=Promise.resolve();
  const {panel,inner}= _terminalEls();
  if(!panel)return null;
  clearTimeout(TERMINAL_UI.closeTimer);
  _initTerminalResizeHandle();
  _resetTerminalHeightForViewport();
  _applyClaudeTerminalViewportBounds();
  TERMINAL_UI.open=true;
  TERMINAL_UI.collapsed=false;
  _setTerminalChromeState('expanded');
  panel.classList.add('is-open');
  _syncTerminalTranscriptSpace(true,{immediate:true});
  if(!TERMINAL_UI.resizeObserver&&window.ResizeObserver){
    TERMINAL_UI.resizeObserver=new ResizeObserver(()=>_fitTerminal());
    TERMINAL_UI.resizeObserver.observe(inner||panel);
  }
  const term=_ensureXterm();
  if(term)term.focus();
  return term;
}

async function _resumeClaudeSessionRequest(publicSessionId,epoch){
  let context={epoch,publicSessionId,handle:null,generation:null};
  try{
    const response=await api('/api/claude-code/resume',{method:'POST',body:JSON.stringify({session_id:publicSessionId})});
    if(!_isClaudeTerminalContextCurrent(context,false))return false;
    const handle=response&&typeof response.handle==='string'?response.handle.trim():'';
    const generation=response&&typeof response.generation==='string'?response.generation.trim():'';
    if(!handle||!generation)throw new Error('Claude terminal is unavailable');
    TERMINAL_UI.handle=handle;
    TERMINAL_UI.generation=generation;
    context={...context,handle,generation};
    await Promise.all([
      _mintClaudeTerminalToken('input',context),
      _mintClaudeTerminalToken('resize',context),
    ]);
    if(!_isClaudeTerminalContextCurrent(context))return false;
    if(!_connectClaudeTerminalOutput(context))return false;
    await _resizeClaudeTerminal(context);
    if(!_isClaudeTerminalContextCurrent(context))return false;
    TERMINAL_UI.claudeState='live';
    _setTerminalChromeState(TERMINAL_UI.collapsed?'collapsed':'expanded');
    return true;
  }catch(err){
    if(!_isClaudeTerminalContextCurrent(context,!!context.handle))return false;
    const active=err&&err.status===409;
    showToast(active?t('claude_active_elsewhere'):t('terminal_start_failed')+(err&&err.message?err.message:String(err||'')),3200,active?'warning':'error');
    detachClaudeTerminal();
    return false;
  }
}

function resumeClaudeSession(session){
  if(!session||session.kind!=='claude_code'||!session.session_id||!['qwen','ornith'].includes(session.profile)||!session.can_remote_resume)return Promise.resolve(false);
  if(!_openClaudeTerminalChrome(session))return Promise.resolve(false);
  const request=_resumeClaudeSessionRequest(session.session_id,TERMINAL_UI.claudeOpenEpoch);
  TERMINAL_UI.readyPromise=request;
  return request;
}

function _connectTerminalOutput(){
  const sid=_terminalSessionId();
  if(!sid)return;
  if(TERMINAL_UI.source){
    try{if(TERMINAL_UI.source.readyState!==2)TERMINAL_UI.source.close();}catch(_){}
    TERMINAL_UI.source=null;
  }
  const url=new URL('api/terminal/output',document.baseURI||location.href);
  url.searchParams.set('session_id',sid);
  const source=new EventSource(url.href,{withCredentials:true});
  TERMINAL_UI.source=source;
  source.addEventListener('output',ev=>{
    if(TERMINAL_UI.source!==source)return;
    let text='';
    try{text=(JSON.parse(ev.data)||{}).text||'';}
    catch(_){text=ev.data||'';}
    if(TERMINAL_UI.term&&text)TERMINAL_UI.term.write(text);
    if(text&&window._terminalAutoExpandOnOutput&&TERMINAL_UI.open&&TERMINAL_UI.collapsed)expandComposerTerminal({focus:false});
  });
  source.addEventListener('terminal_closed',()=>{
    if(TERMINAL_UI.source!==source)return;
    if(TERMINAL_UI.term)TERMINAL_UI.term.writeln('\r\n[terminal closed]\r\n');
    try{if(source&&source.readyState!==2)source.close();}catch(_){}
    TERMINAL_UI.source=null;
    setTimeout(()=>closeComposerTerminal(null,{skipApi:true}),260);
  });
  source.addEventListener('terminal_error',ev=>{
    if(TERMINAL_UI.source!==source)return;
    let msg=t('terminal_error');
    try{msg=(JSON.parse(ev.data)||{}).error||msg;}catch(_){}
    if(TERMINAL_UI.term)TERMINAL_UI.term.writeln('\r\n[terminal error] '+msg+'\r\n');
    try{if(source&&source.readyState!==2)source.close();}catch(_){}
    TERMINAL_UI.source=null;
  });
  // A successful (re)connect clears the notify latch so the NEXT outage notifies
  // again — "once per outage", not "once per source lifetime". Without this, a
  // source that errors, auto-reconnects, then drops a second time would stay
  // silent (guard already true, not CLOSED) — the exact freeze this handler
  // prevents.
  source.addEventListener('open',()=>{
    if(TERMINAL_UI.source!==source)return;
    source._terminalErrNotified=false;
  });
  // Transport-level failures (session expired, gateway killed, network drop)
  // fire 'error' rather than a terminal_* event; without this the terminal froze
  // with no feedback and no telemetry. Let the browser auto-reconnect a merely
  // CONNECTING source (no manual loop/backoff), but surface a permanently CLOSED
  // one and tear it down so a restart can reconnect. Notify once per outage so a
  // flapping connection can't flood the pane or telemetry.
  source.addEventListener('error',()=>{
    if(TERMINAL_UI.source!==source)return;
    const closed=source.readyState===EventSource.CLOSED;
    if(closed||!source._terminalErrNotified){
      source._terminalErrNotified=true;
      if(typeof recordClientSSEError==='function')recordClientSSEError('terminal',{ready_state:source?source.readyState:null,reason:'terminal EventSource.onerror'});
      if(TERMINAL_UI.term)TERMINAL_UI.term.writeln('\r\n[terminal '+(closed?'disconnected':'connection lost, reconnecting…')+']\r\n');
    }
    if(closed){
      try{if(source&&source.readyState!==2)source.close();}catch(_){}
      TERMINAL_UI.source=null;
    }
  });
}

async function _startComposerTerminal(restart=false){
  const sid=_terminalSessionId();
  if(!sid||!(S.session&&S.session.workspace)){
    showToast(t('terminal_no_workspace_title'),2600,'warning');
    syncTerminalButton();
    return;
  }
  if(S.terminalRemoteBackend){
    showToast(_terminalRemoteBackendUnsupportedMessage(),3200,'warning');
    syncTerminalButton();
    return;
  }
  const term=_ensureXterm();
  if(!term)return;
  _fitTerminal();
  const dims=_terminalDimensions();
  try{
    await api('/api/terminal/start',{method:'POST',body:JSON.stringify({
      session_id:sid,
      rows:dims.rows,
      cols:dims.cols,
      restart:!!restart,
    })});
  }catch(e){
    e.message=_terminalStartErrorMessage(e);
    throw e;
  }
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
    const messages=_terminalMessagesEl();
    if(!panel)return;
    clearTimeout(TERMINAL_UI.closeTimer);
    _initTerminalResizeHandle();
    _resetTerminalHeightForViewport();
    if(messages)messages.classList.add('terminal-expanding-from-dock');
    _setTerminalChromeState('expanded');
    TERMINAL_UI.open=true;
    TERMINAL_UI.collapsed=false;
    _syncTerminalTranscriptSpace(true,{immediate:true});
    if(messages)void messages.offsetHeight;
    requestAnimationFrame(()=>{
      panel.classList.add('is-open');
      window.setTimeout(_fitTerminal,80);
      setTimeout(()=>{
        if(messages)messages.classList.remove('terminal-expanding-from-dock');
      },120);
    });
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

function expandComposerTerminal(opts){
  if(!TERMINAL_UI.open)return;
  const focus = !opts || opts.focus !== false;
  const {panel}= _terminalEls();
  const messages=_terminalMessagesEl();
  TERMINAL_UI.collapsed=false;
  clearTimeout(TERMINAL_UI.closeTimer);
  if(panel)panel.classList.add('is-expanding-from-dock');
  if(messages)messages.classList.add('terminal-expanding-from-dock');
  _syncTerminalTranscriptSpace(true,{immediate:true});
  if(messages)void messages.offsetHeight;
  _setTerminalChromeState('expanded');
  _resetTerminalHeightForViewport();
  requestAnimationFrame(()=>{
    _fitTerminal();
    if(focus) focusComposerTerminalInput();
    setTimeout(()=>{
      if(panel)panel.classList.remove('is-expanding-from-dock');
      if(messages)messages.classList.remove('terminal-expanding-from-dock');
    },120);
  });
  syncTerminalButton();
}

function _disposeXterm(){
  TERMINAL_UI.fontLoadGeneration+=1;
  TERMINAL_UI.fontLoadRequest=null;
  if(TERMINAL_UI.fontFitFrame!==null){
    if(typeof cancelAnimationFrame==='function')cancelAnimationFrame(TERMINAL_UI.fontFitFrame);
    TERMINAL_UI.fontFitFrame=null;
  }
  if(TERMINAL_UI.term){
    try{TERMINAL_UI.term.dispose();}catch(_){}
  }
  TERMINAL_UI.term=null;
  TERMINAL_UI.fitAddon=null;
  TERMINAL_UI.typedLine='';
  TERMINAL_UI.lastAppliedTheme=null;
  TERMINAL_UI.lastAppliedFontFamily=null;
  const {surface}= _terminalEls();
  if(surface)surface.textContent='';
}

function detachClaudeTerminal(){
  if(TERMINAL_UI.mode!=='claude_code')return;
  TERMINAL_UI.claudeOpenEpoch+=1;
  _disconnectTerminalSource();
  const {panel,inner}= _terminalEls();
  if(panel){
    panel.classList.remove('is-open','is-collapsed','is-expanding-from-dock','claude-terminal-mode');
    panel.hidden=true;
    panel.style.removeProperty('--claude-terminal-viewport-height');
  }
  if(inner){
    inner.style.removeProperty('--claude-terminal-height');
    inner.style.removeProperty('--claude-terminal-min-height');
    inner.style.removeProperty('--claude-terminal-max-height');
  }
  _syncTerminalTranscriptSpace(false);
  _disposeXterm();
  TERMINAL_UI.open=false;
  TERMINAL_UI.collapsed=false;
  TERMINAL_UI.mode='shell';
  TERMINAL_UI.claudePublicSessionId=null;
  TERMINAL_UI.claudeSession=null;
  TERMINAL_UI.claudeState='idle';
  TERMINAL_UI.claudeCursor=null;
  TERMINAL_UI.handle=null;
  TERMINAL_UI.generation=null;
  TERMINAL_UI.claudeLabel=null;
  TERMINAL_UI.readyPromise=null;
  TERMINAL_UI.inputQueue=Promise.resolve();
  TERMINAL_UI.sessionId=null;
  TERMINAL_UI.workspace=null;
  const composerWrap=$('composerWrap');
  if(composerWrap)composerWrap.classList.remove('terminal-dock-visible');
  syncTerminalButton();
}

function syncClaudeTerminalNavigation(sessionId){
  if(TERMINAL_UI.mode==='claude_code'
    &&TERMINAL_UI.claudePublicSessionId!==sessionId)detachClaudeTerminal();
}

async function stopClaudeTerminal(){
  const identity=_claudeTerminalIdentity();
  if(!identity)return;
  const context=_claudeTerminalContext(identity);
  const confirmed=await showConfirmDialog({
    title:t('claude_terminal_stop_title'),
    message:t('claude_terminal_stop_message'),
    confirmLabel:t('claude_terminal_stop'),
    danger:true,
    focusCancel:true,
  });
  if(!confirmed)return;
  if(!_isClaudeTerminalContextCurrent(context))return;
  try{
    await _mintClaudeTerminalToken('stop',identity);
    if(!_isClaudeTerminalContextCurrent(context))return;
    await api('/api/claude-code/stop',{method:'POST',retries:0,body:JSON.stringify(identity)});
    if(!_isClaudeTerminalContextCurrent(context))return;
    detachClaudeTerminal();
    showToast(t('claude_terminal_stopped'));
  }catch(err){
    showToast(t('terminal_error')+': '+(err&&err.message?err.message:String(err||'')),3200,'error');
  }
}

async function closeComposerTerminal(sessionId,opts){
  if(TERMINAL_UI.mode==='claude_code'){
    detachClaudeTerminal();
    return;
  }
  opts=opts||{};
  const sid=sessionId||TERMINAL_UI.sessionId||_terminalSessionId();
  if(TERMINAL_UI.source){
    try{if(TERMINAL_UI.source&&TERMINAL_UI.source.readyState!==2)TERMINAL_UI.source.close();}catch(_){}
    TERMINAL_UI.source=null;
  }
  if(sid&&!opts.skipApi){
    api('/api/terminal/close',{method:'POST',body:JSON.stringify({session_id:sid})}).catch(()=>{});
  }
  const {panel}= _terminalEls();
  if(panel){
    panel.classList.remove('is-open','is-collapsed','is-expanding-from-dock');
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
  if(TERMINAL_UI.mode==='claude_code'){
    if(TERMINAL_UI.claudeState!=='live'||!_claudeTerminalIdentity()){
      const session=TERMINAL_UI.claudeSession;
      if(session)await resumeClaudeSession(session);
      return;
    }
    try{await _reconnectClaudeTerminal();}
    catch(e){showToast(t('terminal_start_failed')+(e&&e.message?e.message:String(e||'')),3200,'error');}
    return;
  }
  if(TERMINAL_UI.source){
    try{if(TERMINAL_UI.source&&TERMINAL_UI.source.readyState!==2)TERMINAL_UI.source.close();}catch(_){}
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
  if(TERMINAL_UI.mode==='claude_code'){
    await _resizeClaudeTerminal();
    return;
  }
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

async function _resizeClaudeTerminal(context){
  if(!TERMINAL_UI.open||TERMINAL_UI.collapsed||TERMINAL_UI.mode!=='claude_code')return;
  const identity=_claudeTerminalIdentity();
  if(!identity)return;
  context=context||_claudeTerminalContext(identity);
  if(!_isClaudeTerminalContextCurrent(context))return;
  const dims=_terminalDimensions();
  const resize=()=>api('/api/claude-code/terminal/resize',{method:'POST',retries:0,body:JSON.stringify({
    handle:identity.handle,
    generation:identity.generation,
    rows:dims.rows,
    cols:dims.cols,
  })});
  try{
    await resize();
    return _isClaudeTerminalContextCurrent(context);
  }catch(err){
    if(!_isClaudeTerminalContextCurrent(context))return;
    if(!err||err.status!==404)return;
    try{
      await _mintClaudeTerminalToken('resize',identity);
      if(!_isClaudeTerminalContextCurrent(context))return;
      await resize();
      return _isClaudeTerminalContextCurrent(context);
    }catch(_){ }
  }
}

window.addEventListener('beforeunload',()=>{
  if(TERMINAL_UI.source)try{if(TERMINAL_UI.source&&TERMINAL_UI.source.readyState!==2)TERMINAL_UI.source.close();}catch(_){}
  if(TERMINAL_UI.mode==='claude_code')return;
  if(TERMINAL_UI.sessionId){
    const url=new URL('api/terminal/close',document.baseURI||location.href).href;
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

function _applyClaudeTerminalViewportBounds(){
  if(TERMINAL_UI.mode!=='claude_code'||!window.visualViewport)return;
  const {panel,inner}= _terminalEls();
  const viewportHeight=Math.max(1,Math.floor(Number(window.visualViewport.height)||0));
  const available=Math.max(1,viewportHeight-96);
  const minHeight=Math.min(72,available);
  const preferred=Number(TERMINAL_UI.height)||TERMINAL_MOBILE_HEIGHT_DEFAULT;
  const height=Math.max(minHeight,Math.min(preferred,available));
  if(panel)panel.style.setProperty('--claude-terminal-viewport-height',viewportHeight+'px');
  if(inner){
    inner.style.setProperty('--claude-terminal-height',height+'px');
    inner.style.setProperty('--claude-terminal-min-height',minHeight+'px');
    inner.style.setProperty('--claude-terminal-max-height',available+'px');
  }
}

function _handleClaudeTerminalVisualViewport(){
  if(TERMINAL_UI.mode!=='claude_code'||!TERMINAL_UI.open)return;
  const {viewport}= _terminalEls();
  _resetTerminalHeightForViewport();
  _applyClaudeTerminalViewportBounds();
  if(viewport&&typeof viewport.scrollIntoView==='function')viewport.scrollIntoView({block:'nearest'});
  _fitTerminal();
}

if(window.visualViewport){
  window.visualViewport.addEventListener('resize',_handleClaudeTerminalVisualViewport);
  window.visualViewport.addEventListener('scroll',_handleClaudeTerminalVisualViewport);
}

if(document&&typeof document.addEventListener==='function'){
  document.addEventListener('visibilitychange',()=>{
    if(TERMINAL_UI.mode!=='claude_code'||!TERMINAL_UI.open)return;
    if(document.hidden){
      TERMINAL_UI.claudeOpenEpoch+=1;
      _disconnectTerminalSource();
      return;
    }
    if(TERMINAL_UI.source)return;
    if(TERMINAL_UI.claudeState==='starting'&&TERMINAL_UI.claudeSession){
      resumeClaudeSession(TERMINAL_UI.claudeSession).catch(()=>{});
      return;
    }
    if(_claudeTerminalIdentity())_reconnectClaudeTerminal().catch(()=>{});
  });
}

if(window.MutationObserver){
  new MutationObserver(()=>syncComposerTerminalAppearance()).observe(document.documentElement,{
    attributes:true,
    attributeFilter:['class','data-skin','style'],
  });

  const terminalHead=document.head;
  if(terminalHead){
    const terminalHeadStylesheetLoadListener=(event)=>{
      const target=event && event.target;
      if(!target||typeof target.tagName!=='string'||target.tagName.toLowerCase()!=='link')return;
      const rel=typeof target.getAttribute==='function'
        ? target.getAttribute('rel')
        : target.rel;
      if(!String(rel||'').toLowerCase().split(/\s+/).includes('stylesheet'))return;
      syncComposerTerminalAppearance(true);
    };
    terminalHead.addEventListener('load',terminalHeadStylesheetLoadListener,true);
    new MutationObserver(()=>syncComposerTerminalAppearance(true)).observe(terminalHead,{
      attributes:true,
      attributeFilter:['href','media','disabled'],
      childList:true,
      subtree:true,
      characterData:true,
    });
  }
}
