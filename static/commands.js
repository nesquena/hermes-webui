// ── Slash commands ──────────────────────────────────────────────────────────
// Built-in commands intercepted before send(). Each command runs locally
// (no round-trip to the agent) and shows feedback via toast or local message.

const COMMANDS=[
  {name:'help',      desc:t('cmd_help'),             fn:cmdHelp},
  {name:'clear',     desc:t('cmd_clear'),         fn:cmdClear},
  {name:'compress',  desc:t('cmd_compress'),       fn:cmdCompress, arg:'[focus topic]'},
  {name:'compact',   desc:t('cmd_compact_alias'),       fn:cmdCompact},
  {name:'model',     desc:t('cmd_model'),  fn:cmdModel,     arg:'model_name'},
  {name:'workspace', desc:t('cmd_workspace'),            fn:cmdWorkspace, arg:'name'},
  {name:'new',       desc:t('cmd_new'),            fn:cmdNew},
  {name:'usage',     desc:t('cmd_usage'),   fn:cmdUsage},
  {name:'theme',     desc:t('cmd_theme'), fn:cmdTheme, arg:'name'},
  {name:'personality', desc:t('cmd_personality'), fn:cmdPersonality, arg:'name'},
  {name:'skills', desc:t('cmd_skills'), fn:cmdSkills, arg:'query'},
];

function parseCommand(text){
  if(!text.startsWith('/'))return null;
  const parts=text.slice(1).split(/\s+/);
  const name=parts[0].toLowerCase();
  const args=parts.slice(1).join(' ').trim();
  return {name,args};
}

function executeCommand(text){
  const parsed=parseCommand(text);
  if(!parsed)return false;
  const cmd=COMMANDS.find(c=>c.name===parsed.name);
  if(!cmd)return false;
  cmd.fn(parsed.args);
  return true;
}

function getMatchingCommands(prefix){
  const q=prefix.toLowerCase();
  return COMMANDS.filter(c=>c.name.startsWith(q));
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
      const summaryRef=summary&&typeof summary.reference_message==='string' ? String(summary.reference_message||'').trim() : '';
      const referenceText=summaryRef || (referenceMsg?msgContent(referenceMsg)||String(referenceMsg.content||''):'');
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
    showToast(t('personality_set')+name);
  }catch(e){showToast(t('failed_colon')+e.message);}
}

// ── Autocomplete dropdown ───────────────────────────────────────────────────

let _cmdSelectedIdx=-1;

function showCmdDropdown(matches){
  const dd=$('cmdDropdown');
  if(!dd)return;
  dd.innerHTML='';
  _cmdSelectedIdx=-1;
  for(let i=0;i<matches.length;i++){
    const c=matches[i];
    const el=document.createElement('div');
    el.className='cmd-item';
    el.dataset.idx=i;
    const usage=c.arg?` <span class="cmd-item-arg">${esc(c.arg)}</span>`:'';
    el.innerHTML=`<div class="cmd-item-name">/${esc(c.name)}${usage}</div><div class="cmd-item-desc">${esc(c.desc)}</div>`;
    el.onmousedown=(e)=>{
      e.preventDefault();
      $('msg').value='/'+c.name+(c.arg?' ':'');
      hideCmdDropdown();
      $('msg').focus();
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
