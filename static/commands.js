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
  {name:'new',       desc:t('cmd_new'),            fn:cmdNew,       noEcho:true},
  {name:'usage',     desc:t('cmd_usage'),   fn:cmdUsage,     noEcho:true},
  {name:'theme',     desc:t('cmd_theme'), fn:cmdTheme, arg:'name',  noEcho:true},
  {name:'personality', desc:t('cmd_personality'), fn:cmdPersonality, arg:'name', subArgs:'personalities'},
  {name:'skills',    desc:t('cmd_skills'),   fn:cmdSkills,   arg:'query'},
  {name:'stop',      desc:t('cmd_stop'),     fn:cmdStop,      noEcho:true},
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
  return matches;
}

let _slashModelCache=null;
let _slashModelCachePromise=null;
let _slashPersonalityCache=null;
let _slashPersonalityCachePromise=null;

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

async function cmdNew(){
  if(typeof clearCompressionUi==='function') clearCompressionUi();
  await newSession();
  await renderSessionList();
  $('msg').focus();
  showToast(t('new_session'));
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
    const visibleMessages=(S.messages||[]).filter(m=>{
      if(!m||!m.role||m.role==='tool') return false;
      if(m.role==='assistant'){
        const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
        const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
        if(hasTc||hasTu|| (typeof _messageHasReasoningPayload==='function' && _messageHasReasoningPayload(m))) return true;
      }
      return typeof msgContent==='function' ? !!msgContent(m) || !!m.attachments?.length : !!m.content || !!m.attachments?.length;
    });
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
    const data=await api('/api/session/compress',{method:'POST',body:JSON.stringify(body)});
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
        commandText:effectiveFocus?`/compress ${effectiveFocus}`:'/compress',
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
  if(!S.busy){showToast(t('cmd_queue_not_busy'));return;}
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
  if(!S.busy||!S.activeStreamId){showToast(t('no_active_task'));return;}
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
 * /steer <message> — Inject a steering hint mid-task.
 * Currently falls back to interrupt behaviour because the WebUI cannot
 * inject messages into an in-flight agent thread.  Shows a toast to
 * inform the user that true steering is not yet available.
 */
async function cmdSteer(args){
  const msg=(args||'').trim();
  if(!msg){showToast(t('cmd_steer_no_msg'));return;}
  if(!S.busy||!S.activeStreamId){showToast(t('no_active_task'));return;}
  if(!S.session){showToast(t('no_active_session'));return;}
  // True steer (inject without cancelling) requires agent-side support
  // that is not yet available in the WebUI.  Fall back to interrupt.
  queueSessionMessage(S.session.session_id,{text:msg,files:[...S.pendingFiles],model:S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'',profile:S.activeProfile||'default'});
  updateQueueBadge(S.session.session_id);
  S.pendingFiles=[];renderTray();
  if(typeof cancelStream==='function'){await cancelStream();}
  showToast(t('cmd_steer_fallback'),2500);
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
    if(data&&data.session){S.messages=data.session.messages||[];S.toolCalls=[];if(typeof clearLiveToolCards==='function')clearLiveToolCards();renderMessages();}
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
    if(data&&data.session){S.messages=data.session.messages||[];S.toolCalls=[];if(typeof clearLiveToolCards==='function')clearLiveToolCards();renderMessages();}
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
async function cmdStatus(){
  if(!S.session){showToast(t('no_active_session'));return;}
  try{
    const r=await api('/api/session/status?session_id='+encodeURIComponent(S.session.session_id));
    if(r&&r.error){showToast(r.error);return;}
    S.messages.push({role:'assistant',content:[`**${t('status_heading')}**`,'',`**${t('status_session_id')}:** \`${r.session_id}\``,`**${t('status_title')}:** ${r.title||t('untitled')}`,`**${t('status_model')}:** ${r.model||t('usage_default_model')}`,`**${t('status_workspace')}:** ${r.workspace}`,`**${t('status_personality')}:** ${r.personality||t('usage_personality_none')}`,`**${t('status_messages')}:** ${r.message_count}`,`**${t('status_agent_running')}:** ${r.agent_running?t('status_yes'):t('status_no')}`,].join('\n')});
    renderMessages();
  }catch(e){showToast(t('status_load_failed')+e.message);}
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
