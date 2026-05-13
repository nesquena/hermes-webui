// ── Slash commands ──────────────────────────────────────────────────────────
// Built-in commands intercepted before send(). Each command runs locally
// (no round-trip to the agent) and shows feedback via toast or local message.

const COMMANDS=[
  // noEcho:true = action-only commands that don't produce a chat response.
  // Commands without noEcho get a user message echoed to the chat (#840).
  {name:'help',      desc:t('cmd_help'),             fn:cmdHelp},
  {name:'clear',     desc:t('cmd_clear'),         fn:cmdClear,     noEcho:true},
  {name:'compress',  desc:t('cmd_compress'),       fn:cmdCompress, arg:'[focus topic]', noEcho:true},
  {name:'compact',   desc:t('cmd_compact_alias'),       fn:cmdCompact, noEcho:true},
  {name:'model',     desc:t('cmd_model'),  fn:cmdModel,     arg:'model_name', subArgs:'models', noEcho:true},
  {name:'workspace', desc:t('cmd_workspace'),            fn:cmdWorkspace, arg:'name',           noEcho:true},
  {name:'terminal',  desc:t('cmd_terminal'),             fn:cmdTerminal,                        noEcho:true},
  {name:'new',       desc:t('cmd_new'),            fn:cmdNew,       noEcho:true},
  {name:'usage',     desc:t('cmd_usage'),   fn:cmdUsage,     noEcho:true},
  {name:'theme',     desc:t('cmd_theme'), fn:cmdTheme, arg:'name',  noEcho:true},
  {name:'personality', desc:t('cmd_personality'), fn:cmdPersonality, arg:'name', subArgs:'personalities'},
  {name:'skills',    desc:t('cmd_skills'),   fn:cmdSkills,   arg:'query'},
  {name:'stop',      desc:t('cmd_stop'),     fn:cmdStop,      noEcho:true},
  {name:'goal',      desc:t('cmd_goal'),     fn:cmdGoal,      arg:'[status|pause|resume|clear|text]', subArgs:['status','pause','resume','clear']},
  {name:'queue',     desc:t('cmd_queue'),    fn:cmdQueue,     arg:'message', noEcho:true},
  {name:'interrupt', desc:t('cmd_interrupt'), fn:cmdInterrupt, arg:'message', noEcho:true},
  {name:'steer',     desc:t('cmd_steer'),    fn:cmdSteer,     arg:'message', noEcho:true},
  {name:'title',     desc:t('cmd_title'),    fn:cmdTitle,    arg:'[title]'},
  {name:'retry',     desc:t('cmd_retry'),    fn:cmdRetry,     noEcho:true},
  {name:'undo',      desc:t('cmd_undo'),     fn:cmdUndo,      noEcho:true},
  {name:'btw',       desc:t('cmd_btw'),      fn:cmdBtw,       arg:'question', noEcho:true},
  {name:'background',desc:t('cmd_background'),fn:cmdBackground,arg:'prompt',  noEcho:true},
  {name:'status',    desc:t('cmd_status'),   fn:cmdStatus},
  {name:'voice',     desc:t('cmd_voice'),    fn:cmdVoice,     noEcho:true},
  {name:'reasoning', desc:t('cmd_reasoning'), fn:cmdReasoning, arg:'show|hide|none|minimal|low|medium|high|xhigh', subArgs:['show','hide','none','minimal','low','medium','high','xhigh'], noEcho:true},
  {name:'yolo', desc:t('cmd_yolo'), fn:cmdYolo, noEcho:true},
  {name:'branch', desc:t('cmd_branch'), fn:cmdBranch, arg:'[name]', noEcho:true},
];

const SLASH_SUBARG_SOURCES={
  model:{desc:t('cmd_model'), subArgs:'models'},
  personality:{desc:t('cmd_personality'), subArgs:'personalities'},
};

function parseCommand(text){
  if(!text.startsWith('/'))return null;
  const parts=text.slice(1).split(/\s+/);
  const name=parts[0].toLowerCase();
  const args=parts.slice(1).join(' ').trim();
  return {name,args};
}

function executeCommand(text){
  const parsed=parseCommand(text);
  if(!parsed)return null;
  const cmd=COMMANDS.find(c=>c.name===parsed.name);
  if(!cmd)return null;
  // A handler may return `false` to opt out of interception — e.g. /reasoning
  // with an effort level falls through so the agent's own handler sees it,
  // preserving the pre-existing pass-through behaviour for that subcommand.
  if(cmd.fn(parsed.args)===false)return null;
  // Return noEcho flag so send() knows whether to echo the command as a user message (#840).
  return {noEcho:!!cmd.noEcho};
}

function getMatchingCommands(prefix){
  const q=prefix.toLowerCase();
  const matches=COMMANDS.filter(c=>c.name.startsWith(q)).map(c=>({...c,source:'builtin'}));
  const seen=new Set(matches.map(c=>c.name));
  for(const [name, spec] of Object.entries(SLASH_SUBARG_SOURCES)){
    if(!name.startsWith(q)||seen.has(name))continue;
    matches.push({
      name,
      desc:spec.desc,
      arg:'name',
      source:'subarg-command',
    });
    seen.add(name);
  }
  for(const skill of _skillCommandCache){
    if(!skill.name.startsWith(q)||seen.has(skill.name))continue;
    matches.push(skill);
    seen.add(skill.name);
  }
  // Include agent/plugin commands from /api/commands metadata
  for(const cmd of (_agentCommandCache||[])){
    const name=String(cmd&&cmd.name||'').toLowerCase();
    if(!name.startsWith(q)||seen.has(name))continue;
    if(cmd.cli_only)continue;
    matches.push({
      name,
      desc:String(cmd&&cmd.description||'').trim()||'Agent command',
      source:cmd.category==='Plugin'?'plugin':'agent',
    });
    seen.add(name);
  }
  return matches;
}

let _slashModelCache=null;
let _slashModelCachePromise=null;
let _slashPersonalityCache=null;
let _slashPersonalityCachePromise=null;
let _agentCommandCache=null;
let _agentCommandCachePromise=null;

// Invalidate the /api/models slash-suggestion cache. Called by panels.js
// after a provider is added or removed so the next /model autocomplete
// rebuilds from a fresh /api/models response (#1539). Returning a function
// rather than letting callers poke the module-local lets/promises directly
// keeps the cache shape encapsulated to this module.
function _invalidateSlashModelCache(){
  _slashModelCache=null;
  _slashModelCachePromise=null;
}
// Expose on window when available. Guarded by typeof so the module is
// importable in headless test contexts (vm.runInContext) that don't
// define a window global — see tests/test_cli_only_slash_commands.py.
if(typeof window!=='undefined'){
  window._invalidateSlashModelCache=_invalidateSlashModelCache;
}

function _normalizeSlashSubArg(value){
  return String(value||'').trim();
}

function _getSlashModelSubArgsFromDom(){
  const sel=$('modelSelect');
  if(!sel) return [];
  const values=[];
  for(const opt of Array.from(sel.options||[])){
    const value=_normalizeSlashSubArg(opt.value||opt.textContent||'');
    if(value) values.push(value);
  }
  return Array.from(new Set(values)).sort((a,b)=>a.localeCompare(b));
}

async function _loadSlashModelSubArgs(force=false){
  const domValues=_getSlashModelSubArgsFromDom();
  if(domValues.length&&!force){
    _slashModelCache=domValues;
    return domValues;
  }
  if(_slashModelCache&&!force) return _slashModelCache;
  if(_slashModelCachePromise&&!force) return _slashModelCachePromise;
  _slashModelCachePromise=(async()=>{
    try{
      const data=await api('/api/models');
      const values=[];
      for(const group of (data&&data.groups)||[]){
        for(const model of (group&&group.models)||[]){
          const id=_normalizeSlashSubArg(model&&model.id);
          if(id) values.push(id);
        }
        // Include extra_models (the catalog tail that doesn't render as
        // <option> entries when the picker is capped) so /model autocomplete
        // covers the full catalog. The trimming is purely a dropdown
        // scannability concern — the slash command exists precisely so
        // power users can reach any model by typing its name. #1567.
        for(const model of (group&&group.extra_models)||[]){
          const id=_normalizeSlashSubArg(model&&model.id);
          if(id) values.push(id);
        }
      }
      const deduped=Array.from(new Set(values)).sort((a,b)=>a.localeCompare(b));
      _slashModelCache=deduped;
      return deduped;
    }catch(_){
      _slashModelCache=domValues;
      return domValues;
    }finally{
      _slashModelCachePromise=null;
    }
  })();
  return _slashModelCachePromise;
}

async function _loadSlashPersonalitySubArgs(force=false){
  if(_slashPersonalityCache&&!force) return _slashPersonalityCache;
  if(_slashPersonalityCachePromise&&!force) return _slashPersonalityCachePromise;
  _slashPersonalityCachePromise=(async()=>{
    try{
      const data=await api('/api/personalities');
      const values=['none'];
      for(const p of (data&&data.personalities)||[]){
        const name=_normalizeSlashSubArg(p&&p.name);
        if(name) values.push(name);
      }
      const deduped=Array.from(new Set(values)).sort((a,b)=>a.localeCompare(b));
      _slashPersonalityCache=deduped;
      return deduped;
    }catch(_){
      _slashPersonalityCache=['none'];
      return _slashPersonalityCache;
    }finally{
      _slashPersonalityCachePromise=null;
    }
  })();
  return _slashPersonalityCachePromise;
}

function _getSlashSubArgOptions(spec){
  if(Array.isArray(spec)) return Promise.resolve(spec.slice());
  if(spec==='models') return _loadSlashModelSubArgs();
  if(spec==='personalities') return _loadSlashPersonalitySubArgs();
  return Promise.resolve([]);
}

let _agentCommandCacheReady=false;
async function loadAgentCommandMetadata(force=false){
  if(_agentCommandCacheReady&&!force)return _agentCommandCache||[];
  if(_agentCommandCachePromise&&!force)return _agentCommandCachePromise;
  _agentCommandCachePromise=(async()=>{
    try{
      const data=await api('/api/commands');
      _agentCommandCache=Array.isArray(data&&data.commands)?data.commands:[];
    }catch(_){
      _agentCommandCache=[];
    }finally{
      _agentCommandCacheReady=true;
      _agentCommandCachePromise=null;
    }
    return _agentCommandCache;
  })();
  return _agentCommandCachePromise;
}

async function getAgentCommandMetadata(name){
  const needle=String(name||'').trim().toLowerCase();
  if(!needle) return null;
  const commands=await loadAgentCommandMetadata();
  return commands.find(cmd=>{
    if(String(cmd&&cmd.name||'').toLowerCase()===needle) return true;
    return Array.isArray(cmd&&cmd.aliases)&&cmd.aliases.some(a=>String(a||'').toLowerCase()===needle);
  })||null;
}

function cliOnlyCommandResponse(cmdName, meta){
  const name=String((meta&&meta.name)||cmdName||'').trim();
  const desc=String((meta&&meta.description)||'').trim();
  const detail=desc?`\n\n${desc}`:'';
  let extra='';
  if(name==='browser'){
    extra='\n\nBrowser tools in WebUI must be configured server-side with the agent/browser environment. Once configured, ask the model to use browser tools directly; `/browser` itself only works in `hermes chat`.';
  }
  return `\`/${name}\` is a Hermes CLI-only command and cannot run inside the WebUI.${detail}${extra}`;
}

async function executeAgentPluginCommand(text,_meta){
  const command=String(text||'').trim();
  if(!command) throw new Error('command is required');
  const data=await api('/api/commands/exec',{
    method:'POST',
    body:JSON.stringify({command})
  });
  return String(data&&data.output||'(no output)');
}

function _parseSlashAutocomplete(text){
  if(!text.startsWith('/')||text.indexOf('\n')!==-1) return null;
  const raw=text.slice(1);
  const hasSpace=/\s/.test(raw);
  const parts=raw.split(/\s+/);
  const cmdName=(parts[0]||'').toLowerCase();
  const command=COMMANDS.find(c=>c.name===cmdName);
  const subArgSource=(command&&command.subArgs)?command:SLASH_SUBARG_SOURCES[cmdName];
  if(!hasSpace||!subArgSource){
    return {kind:'commands', query:raw};
  }
  const argText=raw.slice(cmdName.length).replace(/^\s+/,'');
  return {kind:'subargs', command:{name:cmdName, desc:subArgSource.desc, subArgs:subArgSource.subArgs}, query:argText.toLowerCase(), rawQuery:argText};
}

async function getSlashAutocompleteMatches(text){
  const parsed=_parseSlashAutocomplete(text);
  if(!parsed) return [];
  if(parsed.kind==='commands') return getMatchingCommands(parsed.query);
  const options=await _getSlashSubArgOptions(parsed.command.subArgs);
  return options
    .filter(opt=>String(opt).toLowerCase().startsWith(parsed.query))
    .map(opt=>({
      name:parsed.command.name,
      value:String(opt),
      desc:parsed.command.desc,
      source:'subarg',
      parent:parsed.command.name,
    }));
}

function _compressionAnchorMessageKey(m){
  if(!m||!m.role||m.role==='tool') return null;
  let content='';
  try{
    content=typeof msgContent==='function' ? String(msgContent(m)||'') : String(m.content||'');
  }catch(_){
    content=String(m.content||'');
  }
  const norm=content.replace(/\s+/g,' ').trim().slice(0,160);
  const ts=m._ts||m.timestamp||null;
  const attachments=Array.isArray(m.attachments)?m.attachments.length:0;
  if(!norm && !attachments && !ts) return null;
  return {role:String(m.role||''), ts, text:norm, attachments};
}

// ── Command handlers ────────────────────────────────────────────────────────

function cmdHelp(){
  const lines=COMMANDS.map(c=>{
    const usage=c.arg ? (String(c.arg).startsWith('[') ? ` ${c.arg}` : ` <${c.arg}>`) : '';
    return `  /${c.name}${usage} — ${c.desc}`;
  });
  const msg={role:'assistant',content:t('available_commands')+'\n'+lines.join('\n')};
  S.messages.push(msg);
  renderMessages();
  showToast(t('type_slash'));
}

function cmdClear(){
  if(!S.session)return;
  S.messages=[];S.toolCalls=[];
  clearLiveToolCards();
  if(typeof clearCompressionUi==='function') clearCompressionUi();
  renderMessages();
  $('emptyState').style.display='';
  showToast(t('conversation_cleared'));
}

async function cmdModel(args){
  if(!args){showToast(t('model_usage'));return;}
  const sel=$('modelSelect');
  if(!sel)return;
  const q=args.toLowerCase();
  // Fuzzy match: find first option whose label or value contains the query
  let match=null;
  for(const opt of sel.options){
    if(opt.value.toLowerCase().includes(q)||opt.textContent.toLowerCase().includes(q)){
      match=opt.value;break;
    }
  }
  if(!match){showToast(t('no_model_match')+`"${args}"`);return;}
  sel.value=match;
  await sel.onchange();
  showToast(t('switched_to')+match);
}

async function cmdWorkspace(args){
  if(!args){showToast(t('workspace_usage'));return;}
  try{
    const data=await api('/api/workspaces');
    const q=args.toLowerCase();
    const ws=(data.workspaces||[]).find(w=>
      (w.name||'').toLowerCase().includes(q)||w.path.toLowerCase().includes(q)
    );
    if(!ws){showToast(t('no_workspace_match')+`"${args}"`);return;}
    if(typeof switchToWorkspace==='function') await switchToWorkspace(ws.path, ws.name||ws.path);
    else showToast(t('switched_workspace')+(ws.name||ws.path));
  }catch(e){showToast(t('workspace_switch_failed')+e.message);}
}

async function cmdTerminal(){
  if(!S.session&&typeof newSession==='function'){
    if(!S._profileSwitchWorkspace&&!S._profileDefaultWorkspace){
      try{
        const data=await api('/api/workspaces');
        const first=(data.workspaces||[])[0];
        S._profileSwitchWorkspace=data.last||(first&&first.path)||null;
      }catch(_){}
    }
    await newSession();
    if(typeof renderSessionList==='function') await renderSessionList();
  }
  if(!S.session||!S.session.workspace){
    showToast(t('terminal_no_workspace_title'),2600,'warning');
    if(typeof syncTerminalButton==='function') syncTerminalButton();
    return;
  }
  if(typeof toggleComposerTerminal==='function') await toggleComposerTerminal(true);
}

async function cmdNew(){
  if(typeof clearCompressionUi==='function') clearCompressionUi();
  await newSession();
  await renderSessionList();
  $('msg').focus();
  showToast(t('new_session'));
}

function _manualCompressionVisibleMessages(){
  return (S.messages||[]).filter(m=>{
    if(!m||!m.role||m.role==='tool') return false;
    if(m.role==='assistant'){
      const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
      const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
      if(hasTc||hasTu|| (typeof _messageHasReasoningPayload==='function' && _messageHasReasoningPayload(m))) return true;
    }
    return typeof msgContent==='function' ? !!msgContent(m) || !!m.attachments?.length : !!m.content || !!m.attachments?.length;
  });
}

function _manualCompressionSleep(ms){
  return new Promise(resolve=>setTimeout(resolve, ms));
}

async function _pollManualCompressionResult(sid){
  let delay=700;
  while(true){
    const data=await api(`/api/session/compress/status?session_id=${encodeURIComponent(sid)}`);
    if(data&&data.status==='done') return data;
    if(data&&data.status==='error'){
      const err=new Error(data.error||'Compression failed');
      err.status=data.error_status||400;
      throw err;
    }
    if(data&&data.status==='idle') throw new Error('Compression job is no longer available');
    await _manualCompressionSleep(delay);
    delay=Math.min(2000, delay+300);
  }
}

async function _applyManualCompressionResult(data, focusTopic, visibleCount, commandText){
  if(data&&data.session){
    const currentSid=S.session&&S.session.session_id;
    if(data.session.session_id&&data.session.session_id!==currentSid){
      await loadSession(data.session.session_id);
    }else{
      S.session=data.session;
      S.messages=data.session.messages||[];
      S.toolCalls=data.session.tool_calls||[];
      clearLiveToolCards();
      localStorage.setItem('hermes-webui-session',S.session.session_id);
      if(typeof _setActiveSessionUrl==='function') _setActiveSessionUrl(S.session.session_id);
      syncTopbar();
      renderMessages();
      await renderSessionList();
      updateQueueBadge(S.session.session_id);
    }
  }
  const summary=data&&data.summary;
  if(typeof setCompressionUi==='function'&&S.session){
    const referenceMsg=(S.messages||[]).find(m=>typeof _isContextCompactionMessage==='function'&&_isContextCompactionMessage(m));
    const messageRef=referenceMsg?msgContent(referenceMsg)||String(referenceMsg.content||''):'';
    const summaryRef=summary&&typeof summary.reference_message==='string' ? String(summary.reference_message||'').trim() : '';
    // Prefer the persisted compaction handoff when it already exists in session state.
    // The short summary fallback is only for environments where that message is unavailable.
    const referenceText=messageRef || summaryRef;
    const effectiveFocus=(data&&data.focus_topic)||focusTopic||'';
    setCompressionUi({
      sessionId:S.session.session_id,
      phase:'done',
      focusTopic:effectiveFocus,
      commandText:effectiveFocus?`/compress ${effectiveFocus}`:(commandText||'/compress'),
      beforeCount:visibleCount,
      summary:summary||null,
      referenceText,
      anchorVisibleIdx: data?.session?.compression_anchor_visible_idx,
      anchorMessageKey: data?.session?.compression_anchor_message_key||null,
    });
  }
  if(typeof setComposerStatus==='function') setComposerStatus('');
  renderMessages();
  if(typeof _setCompressionSessionLock==='function') _setCompressionSessionLock(null);
}

async function resumeManualCompressionForSession(sid){
  if(!sid) return;
  try{
    const status=await api(`/api/session/compress/status?session_id=${encodeURIComponent(sid)}`);
    if(!status||status.status!=='running') return;
    const visibleMessages=_manualCompressionVisibleMessages();
    const visibleCount=visibleMessages.length;
    const anchorMessageKey=_compressionAnchorMessageKey(visibleMessages[visibleMessages.length-1]||null);
    if(typeof setBusy==='function') setBusy(true);
    if(typeof setComposerStatus==='function') setComposerStatus(t('compressing'));
    if(typeof setCompressionUi==='function'){
      setCompressionUi({
        sessionId:sid,
        phase:'running',
        focusTopic:status.focus_topic||'',
        commandText:status.focus_topic?`/compress ${status.focus_topic}`:'/compress',
        beforeCount:visibleCount,
        anchorVisibleIdx:Math.max(0, visibleCount-1),
        anchorMessageKey,
      });
    }
    renderMessages();
    const done=await _pollManualCompressionResult(sid);
    if(!S.session||S.session.session_id!==sid) return;
    await _applyManualCompressionResult(done, status.focus_topic||'', visibleCount, status.focus_topic?`/compress ${status.focus_topic}`:'/compress');
  }catch(e){
    if(S.session&&S.session.session_id===sid&&typeof setCompressionUi==='function'){
      const visibleMessages=_manualCompressionVisibleMessages();
      setCompressionUi({
        sessionId:sid,
        phase:'error',
        focusTopic:'',
        commandText:'/compress',
        beforeCount:visibleMessages.length,
        errorText:`Compression failed: ${e.message}`,
        anchorVisibleIdx:Math.max(0, visibleMessages.length-1),
        anchorMessageKey:null,
      });
      renderMessages();
    }
  }finally{
    if(S.session&&S.session.session_id===sid){
      if(typeof _setCompressionSessionLock==='function') _setCompressionSessionLock(null);
      if(typeof setBusy==='function') setBusy(false);
      if(typeof setComposerStatus==='function') setComposerStatus('');
    }
  }
}

async function _runManualCompression(focusTopic){
  if(!S.session){showToast(t('no_active_session'));return;}
  let visibleCount=0;
  try{
    const sid=S.session.session_id;
    // Preflight: verify the viewed session still exists before compressing.
    // This avoids a confusing "not found" toast when the UI is stale.
    try{
      const live=await api(`/api/session?session_id=${encodeURIComponent(sid)}`);
      if(!live||!live.session||live.session.session_id!==sid){
        throw new Error('session no longer available');
      }
      S.session=live.session;
      S.messages=live.session.messages||[];
      S.toolCalls=live.session.tool_calls||[];
      if(typeof _messagesTruncated!=='undefined') _messagesTruncated=false;
    }catch(preflightErr){
      if(typeof clearCompressionUi==='function') clearCompressionUi();
      if(typeof _setCompressionSessionLock==='function') _setCompressionSessionLock(null);
      if(typeof setBusy==='function') setBusy(false);
      if(typeof setComposerStatus==='function') setComposerStatus('');
      renderMessages();
      showToast('Compression failed: '+(preflightErr.message||'session no longer available'));
      return;
    }
    if(typeof setBusy==='function') setBusy(true);
    const body={session_id:sid};
    if(focusTopic) body.focus_topic=focusTopic;
    const visibleMessages=_manualCompressionVisibleMessages();
    visibleCount=visibleMessages.length;
    const anchorVisibleIdx=Math.max(0, visibleCount - 1);
    const anchorMessageKey=_compressionAnchorMessageKey(visibleMessages[visibleMessages.length-1]||null);
    const commandText=focusTopic?`/compress ${focusTopic}`:'/compress';
    if(typeof setCompressionUi==='function'){
      setCompressionUi({
        sessionId:S.session.session_id,
        phase:'running',
        focusTopic:focusTopic||'',
        commandText,
        beforeCount:visibleCount,
        anchorVisibleIdx,
        anchorMessageKey,
      });
    }
    if(typeof setComposerStatus==='function') setComposerStatus(t('compressing'));
    renderMessages();
    const started=await api('/api/session/compress/start',{method:'POST',body:JSON.stringify(body)});
    if(started&&started.status==='error'){
      const err=new Error(started.error||'Compression failed');
      err.status=started.error_status||400;
      throw err;
    }
    const data=(started&&started.status==='done')?started:await _pollManualCompressionResult(sid);
    await _applyManualCompressionResult(data, focusTopic, visibleCount, commandText);
  }catch(e){
    if(typeof setCompressionUi==='function'){
      const currentSid=S.session&&S.session.session_id;
      setCompressionUi({
        sessionId:currentSid||'',
        phase:'error',
        focusTopic:(focusTopic||'').trim(),
        commandText:focusTopic?`/compress ${focusTopic}`:'/compress',
        beforeCount:(S.messages||[]).filter(m=>m&&m.role&&m.role!=='tool').length,
        errorText:`Compression failed: ${e.message}`,
        anchorVisibleIdx: Math.max(0, visibleCount - 1),
        anchorMessageKey:null,
      });
    }
    if(typeof _setCompressionSessionLock==='function') _setCompressionSessionLock(null);
    if(typeof setBusy==='function') setBusy(false);
    if(typeof setComposerStatus==='function') setComposerStatus('');
    renderMessages();
    showToast('Compression failed: '+e.message);
    return;
  }
  if(typeof setBusy==='function') setBusy(false);
}

async function cmdCompress(args){
  await _runManualCompression((args||'').trim());
}

async function cmdCompact(args){
  await _runManualCompression((args||'').trim());
}

async function cmdUsage(){
  const next=!window._showTokenUsage;
  window._showTokenUsage=next;
  try{
    await api('/api/settings',{method:'POST',body:JSON.stringify({show_token_usage:next})});
  }catch(e){}
  // Update the settings checkbox if the panel is open
  const cb=$('settingsShowTokenUsage');
  if(cb) cb.checked=next;
  renderMessages();
  showToast(next?t('token_usage_on'):t('token_usage_off'));
}

async function cmdTheme(args){
  const themes=['system','dark','light'];
  const skins=(_SKINS||[]).map(s=>s.name.toLowerCase());
  const legacyThemes=Object.keys(_LEGACY_THEME_MAP||{});
  const val=(args||'').toLowerCase().trim();
  // Check if it's a theme
  if(themes.includes(val)||legacyThemes.includes(val)){
    const appearance=_normalizeAppearance(
      val,
      legacyThemes.includes(val)?null:localStorage.getItem('hermes-skin')
    );
    localStorage.setItem('hermes-theme',appearance.theme);
    localStorage.setItem('hermes-skin',appearance.skin);
    _applyTheme(appearance.theme);
    _applySkin(appearance.skin);
    try{await api('/api/settings',{method:'POST',body:JSON.stringify({theme:appearance.theme,skin:appearance.skin})});}catch(e){}
    const sel=$('settingsTheme');
    if(sel)sel.value=appearance.theme;
    const skinSel=$('settingsSkin');
    if(skinSel)skinSel.value=appearance.skin;
    if(typeof _syncThemePicker==='function') _syncThemePicker(appearance.theme);
    if(typeof _syncSkinPicker==='function') _syncSkinPicker(appearance.skin);
    showToast(t('theme_set')+appearance.theme+(legacyThemes.includes(val)?` + ${appearance.skin}`:''));
    return;
  }
  // Check if it's a skin
  if(skins.includes(val)){
    const appearance=_normalizeAppearance(localStorage.getItem('hermes-theme'),val);
    localStorage.setItem('hermes-theme',appearance.theme);
    localStorage.setItem('hermes-skin',appearance.skin);
    _applyTheme(appearance.theme);
    _applySkin(appearance.skin);
    try{await api('/api/settings',{method:'POST',body:JSON.stringify({theme:appearance.theme,skin:appearance.skin})});}catch(e){}
    const sel=$('settingsSkin');
    if(sel)sel.value=appearance.skin;
    const themeSel=$('settingsTheme');
    if(themeSel)themeSel.value=appearance.theme;
    if(typeof _syncThemePicker==='function') _syncThemePicker(appearance.theme);
    if(typeof _syncSkinPicker==='function') _syncSkinPicker(appearance.skin);
    showToast(t('theme_set')+appearance.skin);
    return;
  }
  showToast(t('theme_usage')+themes.join('|')+' | '+skins.join('|')+' | legacy:'+legacyThemes.join('|'));
}

async function cmdSkills(args){
  try{
    const data = await api('/api/skills');
    let skills = data.skills || [];
    if(args){
      const q = args.toLowerCase();
      skills = skills.filter(s =>
        (s.name||'').toLowerCase().includes(q) ||
        (s.description||'').toLowerCase().includes(q) ||
        (s.category||'').toLowerCase().includes(q)
      );
    }
    if(!skills.length){
      const msg = {role:'assistant', content: args ? `No skills matching "${args}".` : 'No skills found.'};
      S.messages.push(msg); renderMessages(); return;
    }
    // Group by category
    const byCategory = {};
    skills.forEach(s => {
      const cat = s.category || 'General';
      if(!byCategory[cat]) byCategory[cat] = [];
      byCategory[cat].push(s);
    });
    const lines = [];
    for(const [cat, items] of Object.entries(byCategory).sort()){
      lines.push(`**${cat}**`);
      items.forEach(s => {
        const desc = s.description ? ` — ${s.description.slice(0,80)}${s.description.length>80?'...':''}` : '';
        lines.push(`  \`${s.name}\`${desc}`);
      });
      lines.push('');
    }
    const header = args
      ? `Skills matching "${args}" (${skills.length}):\n\n`
      : `Available skills (${skills.length}):\n\n`;
    S.messages.push({role:'assistant', content: header + lines.join('\n')});
    renderMessages();
    showToast(t('type_slash'));
  }catch(e){
    showToast('Failed to load skills: '+e.message);
  }
}

async function cmdPersonality(args){
  if(!S.session){showToast(t('no_active_session'));return;}
  if(!args){
    // List available personalities
    try{
      const data=await api('/api/personalities');
      if(!data.personalities||!data.personalities.length){
        showToast(t('no_personalities'));
        return;
      }
      const list=data.personalities.map(p=>`  **${p.name}**${p.description?' — '+p.description:''}`).join('\n');
      S.messages.push({role:'assistant',content:t('available_personalities')+'\n\n'+list+t('personality_switch_hint')});
      renderMessages();
    }catch(e){showToast(t('personalities_load_failed'));}
    return;
  }
  const name=args.trim();
  if(name.toLowerCase()==='none'||name.toLowerCase()==='default'||name.toLowerCase()==='clear'){
    try{
      await api('/api/personality/set',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,name:''})});
      showToast(t('personality_cleared'));
    }catch(e){showToast(t('failed_colon')+e.message);}
    return;
  }
  try{
    const res=await api('/api/personality/set',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,name})});
    S.messages.push({role:'assistant',content:t('personality_set')+`**${name}**`});
    renderMessages();
    showToast(t('personality_set')+name);
  }catch(e){showToast(t('failed_colon')+e.message);}
}

async function cmdStop(){
  if(!S.session){showToast(t('no_active_session'));return;}
  if(!S.activeStreamId){showToast(t('no_active_task'));return;}
  if(typeof cancelStream==='function'){await cancelStream();showToast(t('stream_stopped'));}
  else showToast(t('cancel_unavailable'));
}

async function cmdGoal(args){
  if(!S.session){await newSession();await renderSessionList();}
  if(!S.session||!S.session.session_id){showToast(t('no_active_session'));return;}
  const activeSid=S.session.session_id;
  try{
    const r=await api('/api/goal',{method:'POST',body:JSON.stringify({
      session_id:activeSid,
      args:args||'',
      workspace:S.session.workspace,
      model:S.session.model||($('modelSelect')&&$('modelSelect').value)||'',
      model_provider:S.session.model_provider||null,
      profile:S.activeProfile||S.session.profile||'default',
    })});
    const msg = (() => {
      const raw = String((r && r.message) || '').trim();
      const key = String((r && r.message_key) || '').trim();
      const args = Array.isArray(r && r.message_args) ? r.message_args : [];
      if (raw.includes('\n')) return raw;
      if (key && typeof t === 'function') {
        const translated = String(t(key, ...args));
        if (translated && translated !== key) return translated;
      }
      return raw;
    })();
    if(msg){
      S.messages.push({role:'assistant',content:msg,_ts:Date.now()/1000,_goalStatus:true,_transient:true});
      renderMessages({preserveScroll:true});
      showToast(msg.split('\n')[0],2600);
    }
    if(!r||!r.stream_id)return;
    S.toolCalls=[];
    if(typeof clearLiveToolCards==='function')clearLiveToolCards();
    appendThinking();setBusy(true);
    setComposerStatus(t('goal_working_toward'));
    S.activeStreamId=r.stream_id;
    if(S.session&&S.session.session_id===activeSid){
      S.session.active_stream_id=r.stream_id;
      if(typeof r.pending_started_at==='number')S.session.pending_started_at=r.pending_started_at;
      if(r.effective_model)S.session.model=r.effective_model;
      if(r.effective_model_provider)S.session.model_provider=r.effective_model_provider;
    }
    INFLIGHT[activeSid]={messages:[...S.messages],uploaded:[],toolCalls:[]};
    if(typeof markInflight==='function')markInflight(activeSid,r.stream_id);
    if(typeof saveInflightState==='function')saveInflightState(activeSid,{streamId:r.stream_id,messages:INFLIGHT[activeSid].messages,uploaded:[],toolCalls:[]});
    startApprovalPolling(activeSid);
    startClarifyPolling(activeSid);
    if(typeof _fetchYoloState==='function')_fetchYoloState(activeSid);
    attachLiveStream(activeSid,r.stream_id,[]);
    if(typeof renderSessionList==='function')void renderSessionList();
  }catch(e){
    const err=String((e&&e.message)||e||'Goal command failed');
    S.messages.push({role:'assistant',content:`**Goal command failed:** ${err}`,_ts:Date.now()/1000,_error:true});
    renderMessages({preserveScroll:true});
    showToast(err,3000);
  }
}

// ── Busy-input mode commands ──────────────────────────────────────────────
// These commands let users override the default busy_input_mode setting for a
// specific message.  They are only meaningful while the agent is running.

/**
 * /queue <message> — Explicitly queue a message for the next turn.
 * Works regardless of the busy_input_mode setting.
 */
async function cmdQueue(args){
  const msg=(args||'').trim();
  if(!msg){showToast(t('cmd_queue_no_msg'));return;}
  // If nothing is running, /queue <msg> just sends like a normal message
  if(!S.busy){
    const inp=$('msg');
    if(inp){inp.value=msg;}
    if(typeof send==='function'){await send();}
    return;
  }
  if(!S.session){showToast(t('no_active_session'));return;}
  queueSessionMessage(S.session.session_id,{text:msg,files:[...S.pendingFiles],model:S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'',profile:S.activeProfile||'default'});
  updateQueueBadge(S.session.session_id);
  S.pendingFiles=[];renderTray();
  showToast(t('cmd_queue_confirm'),2000);
}

/**
 * /interrupt <message> — Cancel the current turn and send a new message.
 * Calls cancelStream() then queues the message so the drain picks it up.
 */
async function cmdInterrupt(args){
  const msg=(args||'').trim();
  if(!msg){showToast(t('cmd_interrupt_no_msg'));return;}
  // If nothing is running, /interrupt <msg> just sends like a normal message
  if(!S.busy||!S.activeStreamId){
    const inp=$('msg');
    if(inp){inp.value=msg;}
    if(typeof send==='function'){await send();}
    return;
  }
  if(!S.session){showToast(t('no_active_session'));return;}
  // Queue the message first (before cancel sets busy=false and drains)
  queueSessionMessage(S.session.session_id,{text:msg,files:[...S.pendingFiles],model:S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'',profile:S.activeProfile||'default'});
  updateQueueBadge(S.session.session_id);
  S.pendingFiles=[];renderTray();
  // Cancel the active stream; setBusy(false) will drain the queue
  if(typeof cancelStream==='function'){await cancelStream();}
  showToast(t('cmd_interrupt_confirm'),2000);
}

/**
 * /steer <message> — Inject a steering hint mid-task without interrupting.
 *
 * Calls POST /api/chat/steer which looks up the cached AIAgent for this
 * session and calls agent.steer(text). The agent's run loop appends the
 * steer text to the next tool-result message so the model sees it on its
 * next iteration — same pathway as the CLI's /steer command.
 *
 * Falls back to interrupt mode when the agent isn't running, isn't cached,
 * or doesn't support steer (older hermes-agent versions).
 */
async function cmdSteer(args){
  const msg=(args||'').trim();
  if(!msg){showToast(t('cmd_steer_no_msg'));return;}
  // If nothing is running, /steer <msg> just sends like a normal message
  if(!S.busy||!S.activeStreamId){
    const inp=$('msg');
    if(inp){inp.value=msg;}
    if(typeof send==='function'){await send();}
    return;
  }
  if(!S.session){showToast(t('no_active_session'));return;}
  await _trySteer(msg, /*explicitSteer=*/true);
}

/**
 * Shared implementation for /steer and the busy_input_mode='steer' path.
 *
 * Tries the real steer endpoint first. On any non-accept response (no cached
 * agent, agent lacks steer, stream dead, etc.) falls back to interrupt+queue:
 * queues the message and cancels the stream so the drain re-sends it.
 *
 * @param {string} msg - The steer text.
 * @param {boolean} explicitSteer - True if the user explicitly invoked /steer
 *   (vs the busy-mode auto-fallback). Affects toast wording only.
 */
function _showSteerIndicator(text){
  const inner=document.getElementById('msgInner');
  if(!inner) return;
  // Remove any existing steer indicator
  const old=inner.querySelector('.steer-indicator');
  if(old) old.remove();
  const el=document.createElement('div');
  el.className='steer-indicator';
  const badge=document.createElement('span');
  badge.className='steer-badge';
  badge.textContent='Steer';
  const body=document.createElement('span');
  body.className='steer-body';
  body.textContent=text.length>120?text.slice(0,117)+'…':text;
  el.appendChild(badge);
  el.appendChild(body);
  inner.appendChild(el);
  if(typeof scrollToBottom==='function') scrollToBottom();
}

async function _trySteer(msg, explicitSteer){
  let result=null;
  try{
    result=await api('/api/chat/steer',{
      method:'POST',
      body:JSON.stringify({session_id:S.session.session_id,text:msg}),
    });
  }catch(e){
    // Network or server error — fall back to interrupt
    result={accepted:false, fallback:'network_error'};
  }
  if(result&&result.accepted){
    // Show a transient steer indicator in the chat (NOT in S.messages — it must
    // survive the done event's S.messages=d.session.messages replacement).
    // The indicator self-removes when the turn completes (done/cancel/error
    // all call renderMessages which rebuilds msgInner).
    _showSteerIndicator(msg);
    showToast(t('cmd_steer_delivered'),2500);
    return;
  }
  // Fall back to interrupt: queue the message + cancel the stream so the
  // drain in setBusy(false) re-sends it as a fresh turn.
  queueSessionMessage(S.session.session_id,{text:msg,files:[...S.pendingFiles],model:S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'',profile:S.activeProfile||'default'});
  updateQueueBadge(S.session.session_id);
  S.pendingFiles=[];renderTray();
  if(typeof cancelStream==='function'){await cancelStream();}
  // Toast wording differs based on why we're falling back so the user
  // understands what just happened.
  const reason=(result&&result.fallback)||'unknown';
  if(explicitSteer){
    showToast(t('cmd_steer_fallback'),2500);
  } else if(reason==='no_cached_agent'||reason==='not_running'||reason==='stream_dead'){
    // Busy mode hit the steer path before the agent was ready —
    // interrupt is the natural fallback, no need to call out steer.
    showToast(t('busy_interrupt_confirm'),2000);
  } else {
    showToast(t('busy_steer_fallback'),2500);
  }
}

async function cmdTitle(args){
  if(!S.session){showToast(t('no_active_session'));return;}
  const name=(args||'').trim();
  if(!name){
    S.messages.push({role:'assistant',content:`${t('title_current')}: **${S.session.title||t('untitled')}**\n\n${t('title_change_hint')}`});
    renderMessages();return;
  }
  try{
    const r=await api('/api/session/rename',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,title:name})});
    if(r&&r.error){showToast(r.error);return;}
    S.session.title=(r&&r.session&&r.session.title)||name;
    if(typeof syncTopbar==='function')syncTopbar();
    if(typeof renderSessionList==='function')renderSessionList();
    showToast(`${t('title_set')} "${S.session.title}"`);
    S.messages.push({role:'assistant',content:`${t('title_set')} **${S.session.title}**`});
    renderMessages();
  }catch(e){showToast(t('failed_colon')+e.message);}
}
async function cmdRetry(){
  if(!S.session){showToast(t('no_active_session'));return;}
  if(S.session.is_cli_session){showToast(t('cmd_webui_only_session'));return;}
  const activeSid=S.session.session_id;
  try{
    const r=await api('/api/session/retry',{method:'POST',body:JSON.stringify({session_id:activeSid})});
    if(r&&r.error){showToast(r.error);return;}
    if(!S.session||S.session.session_id!==activeSid)return;
    const data=await api('/api/session?session_id='+encodeURIComponent(activeSid));
    if(data&&data.session){S.messages=data.session.messages||[];S.toolCalls=[];if(typeof clearLiveToolCards==='function')clearLiveToolCards();if(typeof _messagesTruncated!=='undefined')_messagesTruncated=false;renderMessages();}
    $('msg').value=r.last_user_text||'';if(typeof autoResize==='function')autoResize();await send();
  }catch(e){showToast(t('retry_failed')+e.message);}
}
async function cmdUndo(){
  if(!S.session){showToast(t('no_active_session'));return;}
  if(S.session.is_cli_session){showToast(t('cmd_webui_only_session'));return;}
  const activeSid=S.session.session_id;
  try{
    const r=await api('/api/session/undo',{method:'POST',body:JSON.stringify({session_id:activeSid})});
    if(r&&r.error){showToast(r.error);return;}
    if(!S.session||S.session.session_id!==activeSid)return;
    const data=await api('/api/session?session_id='+encodeURIComponent(activeSid));
    if(data&&data.session){S.messages=data.session.messages||[];S.toolCalls=[];if(typeof clearLiveToolCards==='function')clearLiveToolCards();if(typeof _messagesTruncated!=='undefined')_messagesTruncated=false;renderMessages();}
    showToast(`↩ ${t('undid_n_messages')} ${r.removed_count} ${t('undid_messages_suffix')}`);
  }catch(e){showToast(t('undo_failed')+e.message);}
}
async function undoLastExchange(){await cmdUndo();}
async function cmdBtw(args){
  if(!S.session){showToast(t('no_active_session'));return;}
  const question=(args||'').trim();
  if(!question){showToast(t('cmd_btw_usage'));return;}
  showToast(t('btw_asking'));
  const activeSid=S.session.session_id;
  try{
    const r=await api('/api/btw',{method:'POST',body:JSON.stringify({session_id:activeSid,question})});
    if(r&&r.error){showToast(r.error);return;}
    // Connect to the ephemeral SSE stream
    const streamId=r.stream_id;
    const parentSid=r.parent_session_id;
    if(typeof attachBtwStream==='function') attachBtwStream(parentSid,streamId,question);
  }catch(e){showToast(t('btw_failed')+e.message);}
}
async function cmdBackground(args){
  if(!S.session){showToast(t('no_active_session'));return;}
  const prompt=(args||'').trim();
  if(!prompt){showToast(t('cmd_background_usage'));return;}
  showToast(t('bg_running'));
  const activeSid=S.session.session_id;
  try{
    const r=await api('/api/background',{method:'POST',body:JSON.stringify({session_id:activeSid,prompt})});
    if(r&&r.error){showToast(r.error);return;}
    // Show background badge and start polling
    if(typeof showBackgroundBadge==='function') showBackgroundBadge(r.task_id);
    if(typeof startBackgroundPolling==='function') startBackgroundPolling(activeSid,r.task_id,prompt);
  }catch(e){showToast(t('bg_failed')+e.message);}
}
function _formatStatusTimestamp(value){
  if(value===undefined||value===null||value==='') return t('status_unknown');
  let date;
  if(typeof value==='number') date=new Date(value < 1000000000000 ? value*1000 : value);
  else date=new Date(value);
  if(Number.isNaN(date.getTime())) return t('status_unknown');
  return date.toLocaleString();
}
function _formatStatusTokens(s){
  const lastUsage=(typeof S!=='undefined'&&(S.lastUsage||s.last_usage))||{};
  const input=Number(s.input_tokens??lastUsage.input_tokens??0)||0;
  const output=Number(s.output_tokens??lastUsage.output_tokens??0)||0;
  const total=Number(s.total_tokens??lastUsage.total_tokens??(input+output))||0;
  const cost=Number(s.estimated_cost??lastUsage.estimated_cost??0)||0;
  if(!total&&!cost) return t('status_no_tokens');
  const fmtNum=n=>Number(n||0).toLocaleString();
  return `${fmtNum(input)} in / ${fmtNum(output)} out${cost?` (~$${cost.toFixed(4)})`:''}`;
}
function _statusProviderForSession(s){
  if(s.model_provider) return String(s.model_provider);
  if(window._activeProvider) return String(window._activeProvider);
  const model=String(s.model||'');
  return model.includes('/') ? model.split('/')[0] : '';
}
function _statusCardFromSession(s){
  const provider=_statusProviderForSession(s);
  const model=s.model||(($('modelSelect')&&$('modelSelect').value)||t('usage_default_model'));
  const running=!!(s.active_stream_id||S.activeStreamId||S.busy);
  const profile=s.profile||S.activeProfile||'default';
  const workspace=s.workspace||S.currentDir||t('status_unknown');
  const rows=[
    {label:t('status_session_id'), value:s.session_id||t('status_unknown')},
    {label:t('status_title'), value:s.title||t('untitled')},
    {label:t('status_model'), value:model},
    {label:t('status_provider'), value:provider||t('status_unknown')},
    {label:t('status_profile'), value:profile},
    {label:t('status_workspace'), value:workspace},
    {label:t('status_personality'), value:s.personality||t('usage_personality_none')},
    {label:t('status_started'), value:_formatStatusTimestamp(s.created_at)},
    {label:t('status_updated'), value:_formatStatusTimestamp(s.updated_at||s.last_message_at)},
    {label:t('status_tokens'), value:_formatStatusTokens(s)},
    {label:t('status_messages'), value:String(s.message_count??(S.messages||[]).filter(m=>m&&m.role&&m.role!=='tool').length)},
    {label:t('status_agent_running'), value:running?t('status_yes'):t('status_no')},
  ];
  return {
    title:t('status_heading'),
    subtitle:t('status_ephemeral'),
    sessionId:s.session_id||'',
    rows,
  };
}
function cmdStatus(){
  if(!S.session){showToast(t('no_active_session'));return;}
  S.messages.push({
    role:'assistant',
    content:'',
    _ephemeral:true,
    _statusCard:_statusCardFromSession(S.session),
    _ts:Date.now()/1000,
  });
  renderMessages();
}
function cmdReasoning(args){
  const arg=(args||'').trim().toLowerCase();
  const BRAIN='\uD83E\uDDE0';
  // Matches hermes_constants.VALID_REASONING_EFFORTS + 'none' (CLI parity).
  const EFFORTS=['none','minimal','low','medium','high','xhigh'];
  // Shared status renderer used by the no-args branch and as a fallback.
  function _fmtStatus(st){
    const vis=(st && st.show_reasoning===false)?'off':'on';
    const eff=(st && st.reasoning_effort)||'default';
    return BRAIN+' Reasoning effort: '+eff+' \u00B7 display: '+vis
      +'  |  /reasoning show|hide|none|minimal|low|medium|high|xhigh';
  }
  if(!arg){
    // Status — read from the same config.yaml keys the CLI uses.
    api('/api/reasoning').then(function(st){showToast(_fmtStatus(st));})
      .catch(function(){showToast(BRAIN+' /reasoning — status unavailable');});
    return true;
  }
  if(arg==='show'||arg==='on'||arg==='hide'||arg==='off'){
    const on=(arg==='show'||arg==='on');
    // Update the UI render gate immediately for responsiveness.
    window._showThinking=on;
    if(typeof renderMessages==='function') renderMessages();
    // Persist via /api/reasoning → config.yaml display.show_reasoning
    // (CLI reads the same key).  Also mirror into WebUI settings.json
    // show_thinking so boot.js picks it up on reload without hitting
    // /api/reasoning on every page load.
    api('/api/reasoning',{method:'POST',body:JSON.stringify({display:arg})}).catch(function(){});
    api('/api/settings',{method:'POST',body:JSON.stringify({show_thinking:on})}).catch(function(){});
    showToast(BRAIN+' Thinking blocks: '+(on?'on':'off')+' (saved)');
    return true;
  }
  if(EFFORTS.includes(arg)){
    // Persist via /api/reasoning → config.yaml agent.reasoning_effort.
    // Takes effect on the NEXT session/turn (agent re-reads config at
    // construction time), matching CLI semantics where `/reasoning high`
    // also forces an agent re-init.
    api('/api/reasoning',{method:'POST',body:JSON.stringify({effort:arg})})
      .then(function(st){
        const eff=(st && st.reasoning_effort)||arg;
        showToast(BRAIN+' Reasoning effort: '+eff+' (saved; applies to next turn)');
        if(typeof _applyReasoningChip==='function') _applyReasoningChip(eff);
      })
      .catch(function(e){
        showToast(BRAIN+' Failed to set effort: '+(e && e.message ? e.message : arg));
      });
    return true;
  }
  showToast('Unknown argument: '+arg+' \u2014 use show|hide|'+EFFORTS.join('|'));
  return true;
}
function cmdVoice(){
  const mic=document.getElementById('btnMic');
  if(mic&&mic.style.display!=='none'&&!mic.disabled){try{mic.click();return;}catch(_){}}
  showToast(t('cmd_voice_use_mic'));
}

// ── YOLO mode toggle ──
// Session-scoped: skips all approval prompts for the current session.
// Toggles on/off; state is not persisted across page reloads.
async function cmdYolo(){
  const sid=S.session&&S.session.session_id;
  if(!sid){showToast(t('yolo_no_session'));return;}
  try{
    // Check current state first to toggle
    const status=await api('/api/session/yolo?session_id='+encodeURIComponent(sid));
    const enable=!status.yolo_enabled;
    await api('/api/session/yolo',{
      method:'POST',
      body:JSON.stringify({session_id:sid,enabled:enable}),
    });
    _yoloEnabled=enable;
    _updateYoloPill();
    showToast(enable?t('yolo_enabled'):t('yolo_disabled'));
    if(enable){
      // Dismiss any visible approval card
      hideApprovalCard(true);
    }
  }catch(e){showToast('YOLO: '+e.message);}
}

// ── Branch / fork command ──
// Forks the current conversation into a new session (#465).
// /branch           → full history copy
// /branch My Name   → full history copy with custom title
async function cmdBranch(args){
  if(!S.session){showToast(t('no_active_session'));return;}
  const customTitle=(args||'').trim()||null;
  try{
    const data=await api('/api/session/branch',{
      method:'POST',
      body:JSON.stringify({
        session_id:S.session.session_id,
        title:customTitle||undefined,
      }),
    });
    if(data&&data.session_id){
      await loadSession(data.session_id);
      if(typeof renderSessionList==='function') await renderSessionList();
      showToast(t('branch_forked'));
    }
  }catch(e){showToast(t('branch_failed')+e.message);}
}

// ── Fork from a specific message point ──
// Called from the "Fork from here" button on message hover actions.
async function forkFromMessage(msgIdx){
  if(!S.session||S.busy)return;
  try{
    const data=await api('/api/session/branch',{
      method:'POST',
      body:JSON.stringify({
        session_id:S.session.session_id,
        keep_count:msgIdx,
      }),
    });
    if(data&&data.session_id){
      await loadSession(data.session_id);
      if(typeof renderSessionList==='function') await renderSessionList();
      showToast(t('branch_forked'));
    }
  }catch(e){showToast(t('branch_failed')+e.message);}
}

let _skillCommandCache=[];
let _skillCommandLoadPromise=null;
let _skillCommandCacheReady=false;
function _skillCommandSlug(name){
  const raw=String(name||'').trim().toLowerCase();
  if(!raw)return'';
  return raw.replace(/[\s_]+/g,'-').replace(/[^a-z0-9-]/g,'').replace(/-{2,}/g,'-').replace(/^-+|-+$/g,'');
}
function _buildSkillCommandEntry(skill){
  const skillName=String(skill&&skill.name||'').trim();
  const slug=_skillCommandSlug(skillName);
  if(!slug)return null;
  if(COMMANDS.some(c=>c.name===slug)) return null;
  return{name:slug,desc:String(skill&&skill.description||'').trim()||t('slash_skill_desc'),source:'skill',skillName};
}
async function loadSkillCommands(force=false){
  if(_skillCommandCacheReady&&!force)return _skillCommandCache;
  if(_skillCommandLoadPromise&&!force)return _skillCommandLoadPromise;
  _skillCommandLoadPromise=(async()=>{
    try{
      const data=await api('/api/skills');
      const deduped=new Map();
      for(const skill of (data&&data.skills)||[]){const entry=_buildSkillCommandEntry(skill);if(entry&&!deduped.has(entry.name))deduped.set(entry.name,entry);}
      _skillCommandCache=Array.from(deduped.values()).sort((a,b)=>a.name.localeCompare(b.name));
    }catch(_){_skillCommandCache=[];}
    finally{_skillCommandCacheReady=true;_skillCommandLoadPromise=null;}
    return _skillCommandCache;
  })();
  return _skillCommandLoadPromise;
}
function refreshSlashCommandDropdown(){
  const ta=$('msg');if(!ta)return;
  const text=ta.value||'';
  if(!text.startsWith('/')||text.indexOf('\n')!==-1){hideCmdDropdown();return;}
  getSlashAutocompleteMatches(text).then(matches=>{
    if(($('msg').value||'')!==text) return;
    if(matches.length)showCmdDropdown(matches);else hideCmdDropdown();
  });
}
function ensureSkillCommandsLoadedForAutocomplete(){
  if(_skillCommandCacheReady||_skillCommandLoadPromise)return;
  loadSkillCommands().then(()=>{refreshSlashCommandDropdown();});
  // Also preload agent/plugin command metadata for autocomplete
  if(!_agentCommandCacheReady&&!_agentCommandCachePromise){
    loadAgentCommandMetadata().then(()=>{refreshSlashCommandDropdown();});
  }
}

// ── Autocomplete dropdown ───────────────────────────────────────────────────

let _cmdSelectedIdx=-1;

function showCmdDropdown(matches){
  const dd=$('cmdDropdown');
  if(!dd)return;
  dd.innerHTML='';
  _cmdSelectedIdx=matches.length?0:-1;
  for(let i=0;i<matches.length;i++){
    const c=matches[i];
    const el=document.createElement('div');
    el.className='cmd-item';
    if(i===_cmdSelectedIdx) el.classList.add('selected');
    el.dataset.idx=i;
    const isSubArg=c.source==='subarg';
    const usage=(!isSubArg&&c.arg)?` <span class="cmd-item-arg">${esc(c.arg)}</span>`:'';
    const badge=c.source==='skill'?`<span class="cmd-item-badge cmd-item-badge-skill">${esc(t('slash_skill_badge'))}</span>`:'';
    if(c.source==='skill') el.classList.add('cmd-item-skill');
    const nameHtml=isSubArg
      ? `<div class="cmd-item-name"><span class="cmd-item-parent">/${esc(c.parent)}</span> <span class="cmd-item-subarg">${esc(c.value)}</span></div>`
      : `<div class="cmd-item-name">/${esc(c.name)}${usage}${badge}</div>`;
    const descHtml=`<div class="cmd-item-desc">${esc(c.desc)}</div>`;
    el.innerHTML=`${nameHtml}${descHtml}`;
    el.onmousedown=(e)=>{
      e.preventDefault();
      const nextValue=isSubArg?('/'+c.parent+' '+c.value):('/'+c.name+(c.arg?' ':''));
      $('msg').value=nextValue;
      $('msg').focus();
      if(!isSubArg&&c.source!=='skill'&&nextValue.endsWith(' ')&&typeof getSlashAutocompleteMatches==='function'){
        getSlashAutocompleteMatches(nextValue).then(matches=>{
          if(($('msg').value||'')!==nextValue) return;
          if(matches.length) showCmdDropdown(matches);
          else hideCmdDropdown();
        });
      }else{
        hideCmdDropdown();
      }
    };
    dd.appendChild(el);
  }
  dd.classList.add('open');
}

function hideCmdDropdown(){
  const dd=$('cmdDropdown');
  if(dd)dd.classList.remove('open');
  _cmdSelectedIdx=-1;
}

function navigateCmdDropdown(dir){
  const dd=$('cmdDropdown');
  if(!dd)return;
  const items=dd.querySelectorAll('.cmd-item');
  if(!items.length)return;
  items.forEach(el=>el.classList.remove('selected'));
  _cmdSelectedIdx+=dir;
  if(_cmdSelectedIdx<0)_cmdSelectedIdx=items.length-1;
  if(_cmdSelectedIdx>=items.length)_cmdSelectedIdx=0;
  items[_cmdSelectedIdx].classList.add('selected');
  // Scroll the newly highlighted item into view so it stays visible when the
  // dropdown overflows and the user navigates with keyboard (#838).
  items[_cmdSelectedIdx].scrollIntoView({block:'nearest'});
}

function selectCmdDropdownItem(){
  const dd=$('cmdDropdown');
  if(!dd)return;
  const items=dd.querySelectorAll('.cmd-item');
  if(_cmdSelectedIdx>=0&&_cmdSelectedIdx<items.length){
    items[_cmdSelectedIdx].onmousedown({preventDefault:()=>{}});
  } else if(items.length===1){
    items[0].onmousedown({preventDefault:()=>{}});
  }
  hideCmdDropdown();
}

// ── Handler aliases (for test-discoverable command registration) ──────────────
// The COMMANDS array above is the authoritative dispatch table. These aliases
// allow tooling and tests to discover command handlers by name independently.
const HANDLERS = {};
HANDLERS.skills = cmdSkills;
