// ── Slash commands ──────────────────────────────────────────────────────────
// Built-in commands intercepted before send(). Each command runs locally
// (no round-trip to the agent) and shows feedback via toast or local message.

const COMMANDS=[
  {name:'help',      desc:'List available commands',             fn:cmdHelp},
  {name:'clear',     desc:'Clear conversation messages',         fn:cmdClear},
  {name:'compact',   desc:'Compress conversation context',       fn:cmdCompact},
  {name:'model',     desc:'Switch model (e.g. /model gpt-4o)',  fn:cmdModel,     arg:'model_name'},
  {name:'workspace', desc:'Switch workspace by name',            fn:cmdWorkspace, arg:'name'},
  {name:'new',       desc:'Start a new chat session',            fn:cmdNew},
  {name:'usage',     desc:'Toggle token usage display on/off',   fn:cmdUsage},
  {name:'theme',     desc:'Switch theme (dark/light/slate/solarized/monokai/nord)', fn:cmdTheme, arg:'name'},
  {name:'personality', desc:'Switch agent personality', fn:cmdPersonality, arg:'name'},
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

// ── Command handlers ────────────────────────────────────────────────────────

function cmdHelp(){
  const lines=COMMANDS.map(c=>{
    const usage=c.arg?` <${c.arg}>`:'';
    return `  /${c.name}${usage} — ${c.desc}`;
  });
  const msg={role:'assistant',content:'**Available commands:**\n'+lines.join('\n')};
  S.messages.push(msg);
  renderMessages();
  showToast('Type / to see commands');
}

function cmdClear(){
  if(!S.session)return;
  S.messages=[];S.toolCalls=[];
  clearLiveToolCards();
  renderMessages();
  $('emptyState').style.display='';
  showToast('Conversation cleared');
}

async function cmdModel(args){
  if(!args){showToast('Usage: /model <name>');return;}
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
  if(!match){showToast(`No model matching "${args}"`);return;}
  sel.value=match;
  await sel.onchange();
  showToast(`Switched to ${match}`);
}

async function cmdWorkspace(args){
  if(!args){showToast('Usage: /workspace <name>');return;}
  try{
    const data=await api('/api/workspaces');
    const q=args.toLowerCase();
    const ws=(data.workspaces||[]).find(w=>
      (w.name||'').toLowerCase().includes(q)||w.path.toLowerCase().includes(q)
    );
    if(!ws){showToast(`No workspace matching "${args}"`);return;}
    if(!S.session)return;
    await api('/api/session/update',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id,workspace:ws.path,model:S.session.model
    })});
    S.session.workspace=ws.path;
    syncTopbar();await loadDir('.');
    showToast(`Switched to workspace: ${ws.name||ws.path}`);
  }catch(e){showToast('Workspace switch failed: '+e.message);}
}

async function cmdNew(){
  await newSession();
  await renderSessionList();
  $('msg').focus();
  showToast('New session created');
}

function cmdCompact(){
  // Send as a regular message to the agent -- the agent's run_conversation
  // preflight will detect the high token count and trigger _compress_context.
  // We send a user message so it appears in the conversation.
  $('msg').value='Please compress and summarize the conversation context to free up space.';
  send();
  showToast('Requesting context compression...');
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
  showToast('Token usage '+(next?'on':'off'));
}

async function cmdTheme(args){
  const themes=['dark','light','slate','solarized','monokai','nord'];
  if(!args||!themes.includes(args.toLowerCase())){
    showToast('Usage: /theme '+themes.join('|'));
    return;
  }
  const t=args.toLowerCase();
  document.documentElement.dataset.theme=t;
  localStorage.setItem('hermes-theme',t);
  try{await api('/api/settings',{method:'POST',body:JSON.stringify({theme:t})});}catch(e){}
  // Update settings dropdown if panel is open
  const sel=$('settingsTheme');
  if(sel)sel.value=t;
  showToast('Theme: '+t);
}

async function cmdPersonality(args){
  if(!S.session){showToast('No active session');return;}
  if(!args){
    // List available personalities
    try{
      const data=await api('/api/personalities');
      if(!data.personalities||!data.personalities.length){
        showToast('No personalities found (add them to ~/.hermes/personalities/)');
        return;
      }
      const list=data.personalities.map(p=>`  **${p.name}**${p.description?' — '+p.description:''}`).join('\n');
      S.messages.push({role:'assistant',content:'Available personalities:\n\n'+list+'\n\nUse `/personality <name>` to switch, or `/personality none` to clear.'});
      renderMessages();
    }catch(e){showToast('Failed to load personalities');}
    return;
  }
  const name=args.trim();
  if(name.toLowerCase()==='none'||name.toLowerCase()==='default'||name.toLowerCase()==='clear'){
    try{
      await api('/api/personality/set',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,name:''})});
      showToast('Personality cleared');
    }catch(e){showToast('Failed: '+e.message);}
    return;
  }
  try{
    const res=await api('/api/personality/set',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,name})});
    showToast('Personality: '+name);
  }catch(e){showToast('Failed: '+e.message);}
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
