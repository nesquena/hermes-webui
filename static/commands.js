// ── Slash commands ──────────────────────────────────────────────────────────
// Registry is sourced from hermes-agent's COMMAND_REGISTRY at boot via
// GET /api/commands. WEBUI_ONLY_COMMANDS (theme, workspace, clear, compact)
// are merged in. Each command's handler comes from HANDLERS below; commands
// without a handler are shown in the dropdown but emit a "not yet
// supported" toast when invoked, rather than being silently sent as
// text to the LLM.

let REGISTRY = [];      // populated by bootCommands() at app start
const HANDLERS = {};    // name -> fn(args), populated below

// Commands implemented purely in the webui (not in agent registry, or
// agent-cli_only and we re-add them here as webui-only).
const WEBUI_ONLY_COMMANDS = [
  // /clear is cli_only in the agent registry (it means "clear terminal
  // screen + new session"). The webui's /clear is a UI-state operation
  // (clear messages display, keep session) -- different semantic, so we
  // own it here.
  {name:'clear',     description:'Clear messages display (keep session)',
   category:'WebUI', aliases:[], args_hint:'',
   subcommands:[], cli_only:false, gateway_only:false},
  {name:'compact',   description:'(deferred) Compress conversation context — coming soon',
   category:'WebUI', aliases:[], args_hint:'',
   subcommands:[], cli_only:false, gateway_only:false},
  {name:'theme',     description:'Change UI theme (webui-only)',
   category:'WebUI', aliases:[], args_hint:'name',
   subcommands:['system','dark','light','slate','solarized','monokai','nord','oled'],
   cli_only:false, gateway_only:false},
  {name:'workspace', description:'Switch active workspace (webui-only)',
   category:'WebUI', aliases:[], args_hint:'name',
   subcommands:[], cli_only:false, gateway_only:false},
];

// Commands the agent exposes but webui cannot/should not surface.
// (These are filtered AFTER /api/commands already excludes gateway_only.)
const UNSUPPORTED_IN_WEBUI = new Set([
  'voice', 'paste', 'image', 'skin', 'browser',
  'platforms', 'plan', 'config', 'tools', 'toolsets', 'plugins',
  'history', 'save',
  // Deferred until webui<->agent IPC is designed:
  'yolo', 'reasoning', 'fast', 'compress',
]);

// Minimal fallback if /api/commands fetch fails (network error, agent
// missing, etc.). Keeps the slash menu functional.
const FALLBACK_REGISTRY = [
  {name:'help',  description:'Show available commands', category:'Info',
   aliases:[], args_hint:'', subcommands:[], cli_only:false, gateway_only:false},
  {name:'clear', description:'Clear messages display (keep session)', category:'WebUI',
   aliases:[], args_hint:'', subcommands:[], cli_only:false, gateway_only:false},
  {name:'new',   description:'Start a new session', category:'Session',
   aliases:['reset'], args_hint:'', subcommands:[], cli_only:false, gateway_only:false},
  {name:'theme', description:'Change UI theme', category:'WebUI',
   aliases:[], args_hint:'name', subcommands:[], cli_only:false, gateway_only:false},
  {name:'workspace', description:'Switch active workspace', category:'WebUI',
   aliases:[], args_hint:'name', subcommands:[], cli_only:false, gateway_only:false},
];

async function bootCommands(){
  let serverList = [];
  try{
    const data = await api('/api/commands');
    serverList = (data && data.commands) || [];
  }catch(e){
    console.warn('Failed to load /api/commands, using fallback', e);
    REGISTRY = FALLBACK_REGISTRY.slice();
    return;
  }
  // Filter: drop cli_only and webui-unsupported.
  serverList = serverList.filter(c =>
    !c.cli_only && !UNSUPPORTED_IN_WEBUI.has(c.name)
  );
  // Merge webui-only commands. If a name collision exists, webui wins.
  const seenNames = new Set(serverList.map(c => c.name));
  const webuiExtras = WEBUI_ONLY_COMMANDS.filter(c => !seenNames.has(c.name));
  REGISTRY = [...serverList, ...webuiExtras];
}

function parseCommand(text){
  if(!text.startsWith('/'))return null;
  const parts=text.slice(1).split(/\s+/);
  const name=parts[0].toLowerCase();
  const args=parts.slice(1).join(' ').trim();
  return {name,args};
}

function _findCommand(name){
  // Direct match
  let cmd = REGISTRY.find(c => c.name === name);
  if(cmd) return cmd;
  // Alias match (registry entries carry their own aliases array from agent)
  cmd = REGISTRY.find(c => c.aliases && c.aliases.includes(name));
  return cmd || null;
}

function executeCommand(text){
  const parsed=parseCommand(text);
  if(!parsed)return false;
  const cmd=_findCommand(parsed.name);
  if(!cmd)return false;  // unknown -- fall through to send as text to LLM
  const handler = HANDLERS[cmd.name];
  if(!handler){
    // Known to registry but not implemented in webui yet.
    // CRITICAL: do NOT fall through to send() -- that would silently forward
    // unknown slash commands as plain text and the LLM would invent fake
    // tool calls.
    showToast(t('cmd_not_supported_yet') + '/' + cmd.name);
    return true;
  }
  handler(parsed.args);
  return true;
}

function getMatchingCommands(prefix){
  const q=prefix.toLowerCase();
  // Match by name OR alias (so typing /reset shows up under /new)
  return REGISTRY.filter(c =>
    c.name.startsWith(q) ||
    (c.aliases && c.aliases.some(a => a.startsWith(q)))
  );
}

// ── Command handlers ────────────────────────────────────────────────────────

function cmdHelp(){
  const lines=REGISTRY.map(c=>{
    const usage=c.args_hint?` <${c.args_hint}>`:'';
    return `  /${c.name}${usage} — ${c.description}`;
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
  await newSession();
  await renderSessionList();
  $('msg').focus();
  showToast(t('new_session'));
}

function cmdCompact(){
  // /compact (formerly: send free text to LLM asking it to compress) is
  // deferred. The agent's /compress requires a full AIAgent instantiation
  // in the webui process -- a separate batch will design that. Show a
  // clear message instead of silently doing the wrong thing.
  showToast(t('cmd_compress_deferred'));
}

async function cmdUsage(){
  if(!S.session){showToast(t('no_active_session'));return;}
  try{
    const r = await api('/api/session/usage?session_id=' + encodeURIComponent(S.session.session_id));
    if(r && r.error){showToast(r.error);return;}
    const cost = (r.estimated_cost != null) ? `$${Number(r.estimated_cost).toFixed(4)}` : t('usage_unknown');
    const lines = [
      `**${t('usage_heading')}**`,
      '',
      `**${t('status_model')}:** ${r.model || t('usage_default_model')}`,
      `**${t('usage_input_tokens')}:** ${r.input_tokens.toLocaleString()}`,
      `**${t('usage_output_tokens')}:** ${r.output_tokens.toLocaleString()}`,
      `**${t('usage_total')}:** ${r.total_tokens.toLocaleString()}`,
      `**${t('usage_estimated_cost')}:** ${cost}`,
      '',
      t('usage_settings_tip'),
    ];
    S.messages.push({role:'assistant', content:lines.join('\n')});
    renderMessages();
  }catch(e){
    showToast(t('usage_load_failed') + e.message);
  }
}

async function cmdTheme(args){
  const themes=['system','dark','light','slate','solarized','monokai','nord','oled'];
  if(!args||!themes.includes(args.toLowerCase())){
    showToast(t('theme_usage')+themes.join('|'));
    return;
  }
  const themeName=args.toLowerCase();
  localStorage.setItem('hermes-theme',themeName);
  _applyTheme(themeName);
  try{await api('/api/settings',{method:'POST',body:JSON.stringify({theme:themeName})});}catch(e){}
  // Update settings dropdown if panel is open
  const sel=$('settingsTheme');
  if(sel)sel.value=themeName;
  showToast(t('theme_set')+themeName);
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

async function cmdStop(){
  if(!S.session){showToast(t('no_active_session'));return;}
  if(!S.activeStreamId){
    // Match agent /stop: shows the no-active-task toast (i18n: no_active_task).
    showToast(t('no_active_task'));
    return;
  }
  // Reuse existing cancelStream() from boot.js -- it already handles
  // cleanup of UI state (cancel button, S.activeStreamId, busy state).
  if(typeof cancelStream === 'function'){
    await cancelStream();
    showToast(t('stream_stopped'));
  }else{
    showToast(t('cancel_unavailable'));
  }
}

async function cmdTitle(args){
  if(!S.session){showToast(t('no_active_session'));return;}
  const name = (args || '').trim();
  if(!name){
    // Match agent: print current title.
    const cur = S.session.title || t('untitled');
    S.messages.push({role:'assistant', content:`${t('title_current')}: **${cur}**\n\n${t('title_change_hint')}`});
    renderMessages();
    return;
  }
  try{
    const r = await api('/api/session/rename',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id, title:name
    })});
    if(r && r.error){showToast(r.error);return;}
    // Server returns r.session.title (already truncated to 80 chars).
    S.session.title = (r && r.session && r.session.title) || name;
    if(typeof syncTopbar === 'function') syncTopbar();
    if(typeof renderSessionList === 'function') renderSessionList();
    showToast(`${t('title_set')} "${S.session.title}"`);
  }catch(e){
    // api() throws Error with message from {error: ...} body on non-2xx.
    showToast(t('failed_colon') + e.message);
  }
}

async function cmdRetry(){
  if(!S.session){showToast(t('no_active_session'));return;}
  // Bridged CLI sessions live in agent's state.db, not webui's JSON store.
  if(S.session.is_cli_session){showToast(t('cmd_webui_only_session'));return;}
  const activeSid = S.session.session_id;
  try{
    const r = await api('/api/session/retry',{method:'POST',body:JSON.stringify({session_id:activeSid})});
    if(r && r.error){showToast(r.error);return;}
    // Race guard: user may have switched sessions during the await.
    if(!S.session || S.session.session_id !== activeSid) return;
    // Refetch transcript to keep frontend in sync with the truncation.
    const data = await api('/api/session?session_id=' + encodeURIComponent(activeSid));
    if(data && data.session){
      S.messages = data.session.messages || [];
      S.toolCalls = [];
      if(typeof clearLiveToolCards === 'function') clearLiveToolCards();
      renderMessages();
    }
    // Stuff the composer with the previous user text and resend.
    $('msg').value = r.last_user_text || '';
    if(typeof autoResize === 'function') autoResize();
    await send();   // existing pipeline -> /api/chat/start
  }catch(e){
    showToast(t('retry_failed') + e.message);
  }
}

async function cmdUndo(){
  if(!S.session){showToast(t('no_active_session'));return;}
  if(S.session.is_cli_session){showToast(t('cmd_webui_only_session'));return;}
  const activeSid = S.session.session_id;
  try{
    const r = await api('/api/session/undo',{method:'POST',body:JSON.stringify({session_id:activeSid})});
    if(r && r.error){showToast(r.error);return;}
    if(!S.session || S.session.session_id !== activeSid) return;
    const data = await api('/api/session?session_id=' + encodeURIComponent(activeSid));
    if(data && data.session){
      S.messages = data.session.messages || [];
      S.toolCalls = [];
      if(typeof clearLiveToolCards === 'function') clearLiveToolCards();
      renderMessages();
    }
    showToast(`↩ ${t('undid_n_messages')} ${r.removed_count} ${t('undid_messages_suffix')}`);
  }catch(e){
    showToast(t('undo_failed') + e.message);
  }
}

async function cmdStatus(){
  if(!S.session){showToast(t('no_active_session'));return;}
  try{
    const r = await api('/api/session/status?session_id=' + encodeURIComponent(S.session.session_id));
    if(r && r.error){showToast(r.error);return;}
    const lines = [
      `**${t('status_heading')}**`,
      '',
      `**${t('status_session_id')}:** \`${r.session_id}\``,
      `**${t('status_title')}:** ${r.title || t('untitled')}`,
      `**${t('status_model')}:** ${r.model || t('usage_default_model')}`,
      `**${t('status_workspace')}:** ${r.workspace}`,
      `**${t('status_personality')}:** ${r.personality || t('usage_personality_none')}`,
      `**${t('status_messages')}:** ${r.message_count}`,
      `**${t('status_agent_running')}:** ${r.agent_running ? t('status_yes') : t('status_no')}`,
    ];
    S.messages.push({role:'assistant', content:lines.join('\n')});
    renderMessages();
  }catch(e){
    showToast(t('status_load_failed') + e.message);
  }
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
    const usage=c.args_hint?` <span class="cmd-item-arg">${esc(c.args_hint)}</span>`:'';
    el.innerHTML=`<div class="cmd-item-name">/${esc(c.name)}${usage}</div><div class="cmd-item-desc">${esc(c.description||'')}</div>`;
    el.onmousedown=(e)=>{
      e.preventDefault();
      $('msg').value='/'+c.name+(c.args_hint?' ':'');
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

// ── Handler registration ───────────────────────────────────────────────────
// Map registry command names to their implementations. Commands not in
// this map will toast "not yet supported" when invoked (via executeCommand).
HANDLERS.help        = cmdHelp;
HANDLERS.clear       = cmdClear;
HANDLERS.new         = cmdNew;
HANDLERS.model       = cmdModel;
HANDLERS.workspace   = cmdWorkspace;
HANDLERS.theme       = cmdTheme;
HANDLERS.personality = cmdPersonality;
HANDLERS.skills      = cmdSkills;
HANDLERS.stop        = cmdStop;
HANDLERS.title       = cmdTitle;
HANDLERS.retry       = cmdRetry;
HANDLERS.undo        = cmdUndo;
HANDLERS.status      = cmdStatus;
HANDLERS.usage       = cmdUsage;
HANDLERS.compact     = cmdCompact;
