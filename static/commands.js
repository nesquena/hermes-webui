// ── Slash commands ──────────────────────────────────────────────────────────
// Built-in commands intercepted before send(). Each command runs locally
// (no round-trip to the agent) and shows feedback via toast or local message.

const COMMANDS=[
  {name:'help',      desc:'查看可用命令',                         fn:cmdHelp},
  {name:'clear',     desc:'清空当前对话消息',                     fn:cmdClear},
  {name:'compact',   desc:'压缩对话上下文',                       fn:cmdCompact},
  {name:'model',     desc:'切换模型（例如 /model gpt-4o）',      fn:cmdModel,     arg:'model_name'},
  {name:'workspace', desc:'按名称切换工作区',                     fn:cmdWorkspace, arg:'name'},
  {name:'new',       desc:'新建聊天会话',                         fn:cmdNew},
  {name:'usage',     desc:'切换 token 用量显示',                  fn:cmdUsage},
  {name:'theme',     desc:'切换主题（dark/light/slate/solarized/monokai/nord/oled）', fn:cmdTheme, arg:'name'},
  {name:'personality', desc:'切换 Agent 人设', fn:cmdPersonality, arg:'name'},
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
  const msg={role:'assistant',content:'**可用命令：**\n'+lines.join('\n')};
  S.messages.push(msg);
  renderMessages();
  showToast('输入 / 可查看命令');
}

function cmdClear(){
  if(!S.session)return;
  S.messages=[];S.toolCalls=[];
  clearLiveToolCards();
  renderMessages();
  $('emptyState').style.display='';
  showToast('对话已清空');
}

async function cmdModel(args){
  if(!args){showToast('用法：/model <name>');return;}
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
  if(!match){showToast(`没有匹配 "${args}" 的模型`);return;}
  sel.value=match;
  await sel.onchange();
  showToast(`已切换到 ${match}`);
}

async function cmdWorkspace(args){
  if(!args){showToast('用法：/workspace <name>');return;}
  try{
    const data=await api('/api/workspaces');
    const q=args.toLowerCase();
    const ws=(data.workspaces||[]).find(w=>
      (w.name||'').toLowerCase().includes(q)||w.path.toLowerCase().includes(q)
    );
    if(!ws){showToast(`没有匹配 "${args}" 的工作区`);return;}
    if(!S.session)return;
    await api('/api/session/update',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id,workspace:ws.path,model:S.session.model
    })});
    S.session.workspace=ws.path;
    syncTopbar();await loadDir('.');
    showToast(`已切换工作区：${ws.name||ws.path}`);
  }catch(e){showToast('工作区切换失败：'+e.message);}
}

async function cmdNew(){
  await newSession();
  await renderSessionList();
  $('msg').focus();
  showToast('已新建会话');
}

function cmdCompact(){
  // Send as a regular message to the agent -- the agent's run_conversation
  // preflight will detect the high token count and trigger _compress_context.
  // We send a user message so it appears in the conversation.
  $('msg').value='Please compress and summarize the conversation context to free up space.';
  send();
  showToast('正在请求压缩上下文...');
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
  showToast('Token 用量显示已'+(next?'开启':'关闭'));
}

async function cmdTheme(args){
  const themes=['dark','light','slate','solarized','monokai','nord','oled'];
  if(!args||!themes.includes(args.toLowerCase())){
    showToast('用法：/theme '+themes.join('|'));
    return;
  }
  const t=args.toLowerCase();
  document.documentElement.dataset.theme=t;
  localStorage.setItem('hermes-theme',t);
  try{await api('/api/settings',{method:'POST',body:JSON.stringify({theme:t})});}catch(e){}
  // Update settings dropdown if panel is open
  const sel=$('settingsTheme');
  if(sel)sel.value=t;
  showToast('主题：'+t);
}

async function cmdPersonality(args){
  if(!S.session){showToast('当前没有活动会话');return;}
  if(!args){
    // List available personalities
    try{
      const data=await api('/api/personalities');
      if(!data.personalities||!data.personalities.length){
        showToast('没有找到人设（可添加到 ~/.hermes/personalities/）');
        return;
      }
      const list=data.personalities.map(p=>`  **${p.name}**${p.description?' — '+p.description:''}`).join('\n');
      S.messages.push({role:'assistant',content:'可用人设：\n\n'+list+'\n\n使用 `/personality <name>` 切换，或用 `/personality none` 清空。'});
      renderMessages();
    }catch(e){showToast('加载人设失败');}
    return;
  }
  const name=args.trim();
  if(name.toLowerCase()==='none'||name.toLowerCase()==='default'||name.toLowerCase()==='clear'){
    try{
      await api('/api/personality/set',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,name:''})});
      showToast('人设已清空');
    }catch(e){showToast('失败：'+e.message);}
    return;
  }
  try{
    const res=await api('/api/personality/set',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,name})});
    showToast('当前人设：'+name);
  }catch(e){showToast('失败：'+e.message);}
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
