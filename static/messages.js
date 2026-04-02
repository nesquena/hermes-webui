// ── Slash command handler ──────────────────────────────────────────────────

const SLASH_COMMANDS = {
  // Session Management
  '/new':       { cat:'Session',     desc:'Start a fresh conversation', fn:_cmdNew },
  '/reset':     { cat:'Session',     desc:'Alias for /new', fn:_cmdNew },
  '/save':      { cat:'Session',     desc:'Create a snapshot of this conversation', fn:_cmdSave },
  '/clear':     { cat:'Session',     desc:'Clear all messages (keeps session)', fn:_cmdClear },
  '/title':     { cat:'Session',     desc:'Set session title (/title My Title)', fn:_cmdTitle },
  '/undo':      { cat:'Session',     desc:'Remove the last exchange', fn:_cmdUndo },
  '/retry':     { cat:'Session',     desc:'Resend the last user message', fn:_cmdRetry },
  '/rollback':  { cat:'Session',     desc:'Restore a previous snapshot (/rollback [N])', fn:_cmdRollback },
  '/stop':      { cat:'Session',     desc:'Cancel the current streaming response', fn:_cmdCancel },
  '/cancel':    { cat:'Session',     desc:'Alias for /stop', fn:_cmdCancel },
  // Model & Config
  '/model':     { cat:'Config',      desc:'Show or switch model (/model <name>)', fn:_cmdModel },
  '/personality':{ cat:'Config',     desc:'Switch personality (/personality <name>)', fn:_cmdPersonality },
  '/reasoning': { cat:'Config',      desc:'Show or set reasoning effort (/reasoning high|medium|low)', fn:_cmdReasoning },
  // Tools & Skills
  '/skills':    { cat:'Tools',       desc:'List installed skills', fn:_cmdSkills },
  '/compress':  { cat:'Tools',       desc:'Summarize conversation to save context', fn:_cmdCompact },
  // Info
  '/usage':     { cat:'Info',        desc:'Show token usage and session stats', fn:_cmdUsage },
  '/insights':  { cat:'Info',        desc:'Show usage analytics (/insights [days])', fn:_cmdInsights },
  '/help':      { cat:'Info',        desc:'Show this help', fn:_cmdHelp },
};

// ── Helpers ──

function _systemMsg(content){
  if(!S.session) return;
  S.messages.push({role:'assistant', content:content, _ts:Date.now()/1000, _system:true});
  renderMessages();
  scrollIfPinned();
}

function _esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function _fmtNum(n){
  if(n>=1e6) return (n/1e6).toFixed(1)+'M';
  if(n>=1e3) return (n/1e3).toFixed(1)+'K';
  return String(n);
}

function _fmtUSD(n){
  if(!n||n<0.0001) return '<$0.0001';
  if(n<0.01) return '$'+n.toFixed(4);
  if(n<1) return '$'+n.toFixed(3);
  return '$'+n.toFixed(2);
}

function _fmtDur(sec){
  if(sec<60) return Math.round(sec)+'s';
  if(sec<3600) return Math.round(sec/60)+'m '+Math.round(sec%60)+'s';
  const h=Math.floor(sec/3600),m=Math.round((sec%3600)/60);
  return h+'h '+m+'m';
}

function _fmtTime(ts){
  if(!ts) return 'n/a';
  const d=new Date(ts*1000);
  return d.toLocaleString();
}

// ── Session Management Commands ──

async function _cmdNew(args){
  await newSession();
  await renderSessionList();
  showToast('New conversation started');
}

async function _cmdClear(args){
  await clearConversation();
}

async function _cmdSave(args){
  if(!S.session){showToast('No active session');return;}
  try{
    const label=args?args.trim():'';
    const data=await api('/api/session/snapshot',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id, label:label||undefined
    })});
    _systemMsg(`**Snapshot saved** \u2014 ${data.label} (${data.message_count} messages, index ${data.snapshot_index})`);
  }catch(e){showToast('Save failed: '+e.message);}
}

async function _cmdTitle(args){
  if(!S.session){showToast('No active session');return;}
  const newTitle=args?args.trim():'';
  if(!newTitle){
    _systemMsg(`**Current title:** ${_esc(S.session.title||'Untitled')}\n\nUsage: \`/title My New Title\``);
    return;
  }
  try{
    await api('/api/session/rename',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id, title:newTitle
    })});
    S.session.title=newTitle;
    syncTopbar();
    renderSessionList();
    showToast('Title updated');
  }catch(e){showToast('Rename failed: '+e.message);}
}

async function _cmdUndo(args){
  if(!S.session){showToast('No active session');return;}
  if(S.messages.length<2){_systemMsg('Nothing to undo.');return;}
  // Remove the last assistant message and the last user message
  let removed=[];
  for(let i=S.messages.length-1;i>=0&&removed.length<2;i--){
    const r=S.messages[i].role;
    if(r==='user'||r==='assistant'){
      removed.push(S.messages.splice(i,1)[0]);
    }
    if(removed.length===2) break;
  }
  if(removed.length===0){_systemMsg('Nothing to undo.');return;}
  // Persist
  try{
    await api('/api/session/truncate',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id, keep_count:S.messages.length
    })});
  }catch(e){}
  renderMessages();
  showToast('Removed last exchange');
}

async function _cmdRetry(args){
  if(!S.session){showToast('No active session');return;}
  if(S.busy){showToast('Wait for current response to finish');return;}
  // Find the last user message
  let lastUserText=null;
  for(let i=S.messages.length-1;i>=0;i--){
    if(S.messages[i].role==='user'){
      lastUserText=S.messages[i].content;
      break;
    }
  }
  if(!lastUserText){_systemMsg('No user message to retry.');return;}
  if(typeof lastUserText!=='string'){
    // Handle structured content
    if(Array.isArray(lastUserText)){
      lastUserText=lastUserText.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('\n');
    } else lastUserText=String(lastUserText);
  }
  // Put it back in the input and send
  $('msg').value=lastUserText;autoResize();
  // Don't return -- fall through to send()
}

async function _cmdRollback(args){
  if(!S.session){showToast('No active session');return;}
  if(S.busy){showToast('Wait for current response to finish');return;}

  // If no args, list available snapshots
  if(!args||!args.trim()){
    try{
      const data=await api('/api/session/snapshots?session_id='+encodeURIComponent(S.session.session_id));
      if(!data.snapshots||!data.snapshots.length){
        _systemMsg('No snapshots available. Use `/save` to create one.');
        return;
      }
      const lines=['**Available snapshots:**\n'];
      for(const s of data.snapshots){
        lines.push(`  **${s.index}** \u2014 ${_esc(s.label)} (${s.message_count} msgs, ${_fmtTime(s.created_at)})`);
      }
      lines.push('\nUse `/rollback <index>` to restore a snapshot.');
      _systemMsg(lines.join('\n'));
    }catch(e){showToast('Failed to list snapshots: '+e.message);}
    return;
  }

  const idx=parseInt(args.trim(),10);
  if(isNaN(idx)){_systemMsg('Usage: `/rollback <index>` (e.g. `/rollback 0`)');return;}
  try{
    const data=await api('/api/session/rollback',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id, snapshot_index:idx
    })});
    S.session=data.session;
    S.messages=data.session.messages||[];
    S.toolCalls=[];
    S.usageStats=null;  // reset usage tracking
    renderMessages();
    syncTopbar();
    showToast(`Rolled back to: ${data.label}`);
  }catch(e){showToast('Rollback failed: '+e.message);}
}

async function _cmdCancel(args){
  if(!S.activeStreamId){showToast('No active stream to cancel');return;}
  try{
    await api('/api/chat/cancel?stream_id='+encodeURIComponent(S.activeStreamId));
    showToast('Stream cancelled');
  }catch(e){showToast('Cancel failed: '+e.message);}
}

// ── Model & Config Commands ──

async function _cmdModel(args){
  if(!args||!args.trim()){
    if(!S.session){showToast('No active session');return;}
    const current=S.session.model||'(default)';
    const sel=$('modelSelect');
    const opts=[];if(sel)for(const o of sel.options)opts.push(o.value);
    _systemMsg(
      `**Current model:** \`${_esc(current)}\`\n\n`+
      `**Available:** ${opts.map(o=>'\`'+_esc(o)+'\`').join(', ')}\n\n`+
      `Use \`/model <name>\` to switch.`
    );
    return;
  }
  const newModel=args.trim();
  if(!S.session){showToast('No active session');return;}
  const sel=$('modelSelect');
  const valid=sel?Array.from(sel.options).map(o=>o.value):[];
  if(valid.length&&!valid.includes(newModel)){
    _systemMsg(`**Unknown model:** \`${_esc(newModel)}\`\n\nAvailable: ${valid.map(o=>'\`'+_esc(o)+'\`').join(', ')}`);
    return;
  }
  S.session.model=newModel;
  if(sel)sel.value=newModel;
  try{
    await api('/api/session/update',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id, model:newModel
    })});
    showToast('Model switched to '+newModel);
    syncTopbar();
  }catch(e){showToast('Failed to switch model: '+e.message);}
}

async function _cmdPersonality(args){
  try{
    const data=await api('/api/personalities');
    const p=data.personalities||{};
    const names=Object.keys(p);
    if(!args||!args.trim()){
      if(!names.length){_systemMsg('No personalities configured in config.yaml.');return;}
      const lines=['**Available personalities:**\n'];
      for(const [name,desc] of Object.entries(p)){
        const preview=String(desc).slice(0,80);
        lines.push(`  **${_esc(name)}** \u2014 ${_esc(preview)}${desc.length>80?'...':''}`);
      }
      lines.push('\nUse `/personality <name>` to switch.');
      _systemMsg(lines.join('\n'));
      return;
    }
    const target=args.trim().toLowerCase();
    if(!p[target]){
      _systemMsg(`**Unknown personality:** \`${_esc(target)}\`\n\nAvailable: ${names.map(n=>'`'+_esc(n)+'`').join(', ')}`);
      return;
    }
    // Switch personality by sending the prompt as a system message to the agent
    _systemMsg(`**Personality switched to:** \`${_esc(target)}\`\n\n> ${_esc(String(p[target]).slice(0,200))}\n\n_Next message will use this personality._`);
    // The personality will take effect on the next message via the LLM
  }catch(e){showToast('Failed to load personalities: '+e.message);}
}

async function _cmdReasoning(args){
  if(!S.session){showToast('No active session');return;}
  const levels=['none','low','medium','high'];
  if(!args||!args.trim()){
    _systemMsg(
      `**Reasoning effort** controls how much the model \"thinks\" before responding.\n\n`+
      `  Levels: \`none\`, \`low\`, \`medium\`, \`high\`\n\n`+
      `Usage: \`/reasoning high\``
    );
    return;
  }
  const level=args.trim().toLowerCase();
  if(!levels.includes(level)){
    _systemMsg(`**Invalid level:** \`${_esc(level)}\`\n\nValid: ${levels.map(l=>'\`'+l+'\`').join(', ')}`);
    return;
  }
  _systemMsg(`**Reasoning effort set to:** \`${level}\`\n\nThis will be applied on your next message.`);
  // Store reasoning preference on the session for next send
  S._reasoningOverride=level;
}

// ── Tools & Skills Commands ──

async function _cmdSkills(args){
  try{
    const data=await api('/api/skills');
    const skills=data.skills||[];
    if(!skills.length){_systemMsg('No skills installed.');return;}
    const lines=['**Installed skills:**\n'];
    // Group by category
    const byCat={};
    for(const s of skills){
      const cat=s.category||'general';
      byCat[cat]=byCat[cat]||[];
      byCat[cat].push(s);
    }
    for(const [cat,items] of Object.entries(byCat)){
      lines.push(`**${_esc(cat)}:**`);
      for(const s of items){
        const desc=(s.description||'').slice(0,60);
        lines.push(`  \`${_esc(s.name)}\` \u2014 ${_esc(desc)}`);
      }
      lines.push('');
    }
    _systemMsg(lines.join('\n'));
  }catch(e){showToast('Failed to load skills: '+e.message);}
}

async function _cmdCompact(args){
  if(!S.session){showToast('No active session');return false;}
  if(S.messages.length<4){_systemMsg('Not enough conversation to compact yet.');return false;}
  return false; // signal: don't consume, send to LLM with rewritten prompt
}

// ── Info Commands ──

async function _cmdUsage(args){
  if(!S.session){showToast('No active session');return;}
  try{
    const data=await api('/api/session/usage?session_id='+encodeURIComponent(S.session.session_id));
    const est=data.estimated_tokens||{};
    const agent=data.agent_usage||{};
    const hasAgent=agent.total_tokens>0||agent.prompt_tokens>0;

    const lines=[
      '**Session Usage**',
      '',
      `\u2501\u2501 Session \u2501\u2501`,
      `  ID: \`${data.session_id.slice(0,12)}\u2026\``,
      `  Model: **${_esc(data.model)}**`,
      `  Workspace: \`${_esc(data.workspace)}\``,
      `  Messages: ${data.message_count}`,
      `  Duration: ${_fmtDur(data.duration_seconds||0)}`,
      `  Created: ${_fmtTime(data.created_at)}`,
      '',
      `\u2501\u2501 Token Estimate \u2501\u2501`,
      `  Total: **${_fmtNum(est.total||0)}**`,
      `  User: ${_fmtNum(est.user||0)}  Assistant: ${_fmtNum(est.assistant||0)}  Tool: ${_fmtNum(est.tool||0)}`,
    ];

    if(hasAgent){
      lines.push('',
        `\u2501\u2501 Agent Usage (API) \u2501\u2501`,
        `  Prompt: ${_fmtNum(agent.prompt_tokens||0)}  Completion: ${_fmtNum(agent.completion_tokens||0)}`,
        `  Total: **${_fmtNum(agent.total_tokens||0)}**`,
        `  API calls: ${agent.api_calls||0}`,
      );
      if(agent.cache_read_tokens||agent.cache_write_tokens){
        lines.push(`  Cache read: ${_fmtNum(agent.cache_read_tokens||0)}  write: ${_fmtNum(agent.cache_write_tokens||0)}`);
      }
      if(agent.reasoning_tokens){
        lines.push(`  Reasoning: ${_fmtNum(agent.reasoning_tokens||0)}`);
      }
      const cost=agent.estimated_cost_usd;
      if(cost){
        lines.push(`  Est. cost: **${_fmtUSD(cost)}**`);
      }
    }

    // Also show frontend-tracked usage if available
    if(S.usageStats&&(S.usageStats.total_tokens||S.usageStats.prompt_tokens)){
      lines.push('',
        `\u2501\u2501 This Session (tracked) \u2501\u2501`,
        `  Total tokens: **${_fmtNum(S.usageStats.total_tokens||0)}**`,
        `  API calls: ${S.usageStats.api_calls||0}`,
      );
      if(S.usageStats.estimated_cost_usd){
        lines.push(`  Est. cost: **${_fmtUSD(S.usageStats.estimated_cost_usd)}**`);
      }
    }

    _systemMsg(lines.join('\n'));
  }catch(e){showToast('Failed to get usage: '+e.message);}
}

async function _cmdInsights(args){
  if(!S.session){showToast('No active session');return;}
  const days=args?parseInt(args.trim(),10):7;
  if(isNaN(days)||days<1){_systemMsg('Usage: `/insights [days]` (e.g. `/insights 30`)');return;}
  try{
    const data=await api('/api/hermes/insights?days='+days);
    if(data.error){_systemMsg('**Error:** '+_esc(data.error));return;}

    const t=data.totals||{};
    const at=data.all_time||{};
    const byModel=data.by_model||{};
    const bySource=data.by_source||{};
    const sessions=data.sessions||[];

    const lines=[
      `**Hermes Insights (last ${data.days} days)**`,
      '',
      `\u2501\u2501 Period Totals \u2501\u2501`,
      `  Sessions: ${t.sessions||0}`,
      `  Messages: ${_fmtNum(t.messages||0)}`,
      `  Tool calls: ${_fmtNum(t.tool_calls||0)}`,
      `  Input tokens: ${_fmtNum(t.input_tokens||0)}`,
      `  Output tokens: ${_fmtNum(t.output_tokens||0)}`,
    ];
    if(t.cache_read_tokens||t.cache_write_tokens){
      lines.push(`  Cache read: ${_fmtNum(t.cache_read_tokens||0)}  write: ${_fmtNum(t.cache_write_tokens||0)}`);
    }
    if(t.reasoning_tokens){
      lines.push(`  Reasoning: ${_fmtNum(t.reasoning_tokens||0)}`);
    }
    if(t.estimated_cost_usd){
      lines.push(`  Est. cost: **${_fmtUSD(t.estimated_cost_usd)}**`);
    }

    // By model
    if(Object.keys(byModel).length>1){
      lines.push('',`\u2501\u2501 By Model \u2501\u2501`);
      for(const [m,c] of Object.entries(byModel)){
        lines.push(`  ${_esc(m)}: ${c} sessions`);
      }
    }

    // By source
    if(Object.keys(bySource).length>=1){
      lines.push('',`\u2501\u2501 By Source \u2501\u2501`);
      for(const [src,c] of Object.entries(bySource)){
        lines.push(`  ${_esc(src)}: ${c} sessions`);
      }
    }

    // All-time
    if(at.sessions){
      lines.push('',
        `\u2501\u2501 All-Time \u2501\u2501`,
        `  Sessions: ${at.sessions}`,
        `  Messages: ${_fmtNum(at.messages||0)}`,
        `  Tool calls: ${_fmtNum(at.tool_calls||0)}`,
        `  Input tokens: ${_fmtNum(at.input_tokens||0)}`,
        `  Output tokens: ${_fmtNum(at.output_tokens||0)}`,
      );
      if(at.estimated_cost_usd){
        lines.push(`  Est. cost: **${_fmtUSD(at.estimated_cost_usd)}**`);
      }
    }

    // Recent sessions list (top 5)
    if(sessions.length){
      lines.push('',`\u2501\u2501 Recent Sessions \u2501\u2501`);
      for(const s of sessions.slice(0,8)){
        const ts=s.started_at?_fmtTime(s.started_at):'n/a';
        const title=(s.title||s.id||'').slice(0,50);
        const tok=((s.input_tokens||0)+(s.output_tokens||0));
        lines.push(`  ${_esc(title)} \u2014 ${s.message_count||0} msgs, ${_fmtNum(tok)} tokens, ${ts}`);
      }
    }

    // Current webui session usage
    if(S.usageStats&&(S.usageStats.total_tokens||S.usageStats.prompt_tokens)){
      lines.push('',
        `\u2501\u2501 This WebUI Session \u2501\u2501`,
        `  Total tokens: ${_fmtNum(S.usageStats.total_tokens||0)}`,
        `  API calls: ${S.usageStats.api_calls||0}`,
      );
    }

    _systemMsg(lines.join('\n'));
  }catch(e){showToast('Failed to get insights: '+e.message);}
}

async function _cmdHelp(args){
  const cats={};
  for(const [cmd,info] of Object.entries(SLASH_COMMANDS)){
    const c=info.cat||'Other';
    cats[c]=cats[c]||[];
    cats[c].push({cmd,desc:info.desc});
  }
  const lines=['**Hermes Web UI \u2014 Slash Commands**\n'];
  for(const [cat,items] of Object.entries(cats)){
    lines.push(`**${cat}:**`);
    for(const it of items){
      lines.push(`  \`${it.cmd}\` \u2014 ${it.desc}`);
    }
    lines.push('');
  }
  lines.push('_Type a command in the message input. Commands are handled locally and never sent to the LLM._');
  _systemMsg(lines.join('\n'));
}

// ── Dispatch ──

// Returns true if handled (don't send to LLM), false if should fall through
async function handleSlashCommand(text){
  const parts=text.split(/\s+/);
  const cmd=parts[0].toLowerCase();
  const args=parts.slice(1).join(' ');

  // Exact match
  if(SLASH_COMMANDS[cmd]){
    const result=await SLASH_COMMANDS[cmd].fn(args);
    return result!==false;
  }

  // Partial / prefix match
  const matches=Object.keys(SLASH_COMMANDS).filter(c=>c.startsWith(cmd));
  if(matches.length===1){
    const result=await SLASH_COMMANDS[matches[0]].fn(args);
    return result!==false;
  }

  // Check if it's a skill name (e.g. /gif-search, /plan)
  const skillName=cmd.slice(1); // strip leading /
  if(skillName.includes('-')||skillName.length>2){
    try{
      const data=await api('/api/skills');
      const skills=data.skills||[];
      const match=skills.find(s=>s.name===skillName||s.name===skillName.replace(/-/g,'-'));
      if(match){
        // Rewrite to a skill trigger prompt and fall through to LLM
        $('msg').value=`/skill ${skillName} ${args}`.trim()+'\nPlease execute the skill: '+skillName+(args?' with context: '+args:'');
        autoResize();
        return false; // send to LLM
      }
    }catch(e){}
  }

  // Unknown
  _systemMsg(`**Unknown command:** \`${_esc(cmd)}\`\n\nType \`/help\` to see available commands.`);
  return true;
}


async function send(){
  const text=$('msg').value.trim();
  if(!text&&!S.pendingFiles.length)return;
  // Don't send while an inline message edit is active
  if(document.querySelector('.msg-edit-area'))return;
  // Intercept slash commands before sending to LLM
  if(text.startsWith('/')){
    $('msg').value='';autoResize();
    const handled=await handleSlashCommand(text);
    if(handled)return;
    // If not consumed, rewrite certain commands to LLM prompts
    const parts=text.split(/\s+/);
    const cmd=parts[0].toLowerCase();
    if(cmd==='/compact'||cmd==='/compress'){
      $('msg').value='Please summarize our conversation so far into a concise summary. List the key topics discussed, decisions made, and any important context I should preserve. Do not use any tools - just provide the summary directly.';
      autoResize();
    } else if(cmd==='/retry'){
      // _cmdRetry already put the text back in the input; fall through to send
    } else {
      // Put original text back for any other fallthrough (e.g. skill triggers)
      $('msg').value=text;autoResize();
    }
  }
  // If busy, queue the message instead of dropping it
  if(S.busy){
    if(text){
      MSG_QUEUE.push(text);
      $('msg').value='';autoResize();
      updateQueueBadge();
      showToast(`Queued: "${text.slice(0,40)}${text.length>40?'\u2026':''}"`,2000);
    }
    return;
  }
  if(!S.session){await newSession();await renderSessionList();}

  const activeSid=S.session.session_id;

  setStatus(S.pendingFiles&&S.pendingFiles.length?'Uploading…':'Sending…');
  let uploaded=[];
  try{uploaded=await uploadPendingFiles();}
  catch(e){if(!text){setStatus(`❌ ${e.message}`);return;}}

  let msgText=text;
  if(uploaded.length&&!msgText)msgText=`I've uploaded ${uploaded.length} file(s): ${uploaded.join(', ')}`;
  else if(uploaded.length)msgText=`${text}\n\n[Attached files: ${uploaded.join(', ')}]`;
  if(!msgText){setStatus('Nothing to send');return;}

  $('msg').value='';autoResize();
  const displayText=text||(uploaded.length?`Uploaded: ${uploaded.join(', ')}`:'(file upload)');
  const userMsg={role:'user',content:displayText,attachments:uploaded.length?uploaded:undefined,_ts:Date.now()/1000};
  S.toolCalls=[];  // clear tool calls from previous turn
  clearLiveToolCards();  // clear any leftover live cards from last turn
  S.messages.push(userMsg);renderMessages();appendThinking();setBusy(true);  // activity bar shown via setBusy
  INFLIGHT[activeSid]={messages:[...S.messages],uploaded};
  startApprovalPolling(activeSid);
  S.activeStreamId = null;  // will be set after stream starts

  // Set provisional title from user message immediately so session appears
  // in the sidebar right away with a meaningful name (server may refine later)
  if(S.session&&(S.session.title==='Untitled'||!S.session.title)){
    const provisionalTitle=displayText.slice(0,64);
    S.session.title=provisionalTitle;
    syncTopbar();
    // Persist it and refresh the sidebar now -- don't wait for done
    api('/api/session/rename',{method:'POST',body:JSON.stringify({
      session_id:activeSid, title:provisionalTitle
    })}).catch(()=>{});  // fire-and-forget, server refines on done
    renderSessionList();  // session appears in sidebar immediately
  } else {
    renderSessionList();  // ensure it's visible even if already titled
  }

  // Start the agent via POST, get a stream_id back
  let streamId;
  try{
    const startData=await api('/api/chat/start',{method:'POST',body:JSON.stringify({
      session_id:activeSid,message:msgText,
      model:S.session.model||$('modelSelect').value,workspace:S.session.workspace,
      attachments:uploaded.length?uploaded:undefined
    })});
    streamId=startData.stream_id;
    S.activeStreamId = streamId;
    markInflight(activeSid, streamId);
    // Show Cancel button
    const cancelBtn=$('btnCancel');
    if(cancelBtn) cancelBtn.style.display='';
  }catch(e){
    delete INFLIGHT[activeSid];
    stopApprovalPolling();
    // Only hide approval card if it belongs to the session that just finished
    if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard();removeThinking();
    S.messages.push({role:'assistant',content:`**Error:** ${e.message}`});
    renderMessages();setBusy(false);setStatus('Error: '+e.message);
    return;
  }

  // Open SSE stream and render tokens live
  let assistantText='';
  let assistantRow=null;
  let assistantBody=null;

  function ensureAssistantRow(){
    if(assistantRow)return;
    removeThinking();
    const tr=$('toolRunningRow');if(tr)tr.remove();
    $('emptyState').style.display='none';
    assistantRow=document.createElement('div');assistantRow.className='msg-row';
    assistantBody=document.createElement('div');assistantBody.className='msg-body';
    const role=document.createElement('div');role.className='msg-role assistant';
    const icon=document.createElement('div');icon.className='role-icon assistant';icon.textContent='H';
    const lbl=document.createElement('span');lbl.style.fontSize='12px';lbl.textContent='Hermes';
    role.appendChild(icon);role.appendChild(lbl);
    assistantRow.appendChild(role);assistantRow.appendChild(assistantBody);
    $('msgInner').appendChild(assistantRow);
  }

  // ── Shared SSE handler wiring (used for initial connection and reconnect) ──
  let _reconnectAttempted=false;

  function _wireSSE(source){
    source.addEventListener('token',e=>{
      if(!S.session||S.session.session_id!==activeSid) return;
      const d=JSON.parse(e.data);
      assistantText+=d.text;
      ensureAssistantRow();
      assistantBody.innerHTML=renderMd(assistantText);
      scrollIfPinned();
    });

    source.addEventListener('tool',e=>{
      const d=JSON.parse(e.data);
      if(S.session&&S.session.session_id===activeSid){
        setStatus(`${d.name}${d.preview?' · '+d.preview.slice(0,55):''}`);
      }
      if(!S.session||S.session.session_id!==activeSid) return;
      removeThinking();
      const oldRow=$('toolRunningRow');if(oldRow)oldRow.remove();
      const tc={name:d.name, preview:d.preview||'', args:d.args||{}, snippet:'', done:false};
      S.toolCalls.push(tc);
      appendLiveToolCard(tc);
      scrollIfPinned();
    });

    source.addEventListener('approval',e=>{
      const d=JSON.parse(e.data);
      d._session_id=activeSid;
      showApprovalCard(d);
    });

    source.addEventListener('done',e=>{
      source.close();
      const d=JSON.parse(e.data);
      delete INFLIGHT[activeSid];
      clearInflight();
      stopApprovalPolling();
      if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard();
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;
        const _cb=$('btnCancel');if(_cb)_cb.style.display='none';
      }
      if(S.session&&S.session.session_id===activeSid){
        S.session=d.session;S.messages=d.session.messages||[];
        if(d.session.tool_calls&&d.session.tool_calls.length){
          S.toolCalls=d.session.tool_calls.map(tc=>({...tc,done:true}));
        } else {
          S.toolCalls=S.toolCalls.map(tc=>({...tc,done:true}));
        }
        if(uploaded.length){
          const lastUser=[...S.messages].reverse().find(m=>m.role==='user');
          if(lastUser)lastUser.attachments=uploaded;
        }
        // Accumulate usage stats from this turn
        if(d.usage){
          if(!S.usageStats) S.usageStats={prompt_tokens:0,completion_tokens:0,
            total_tokens:0,input_tokens:0,output_tokens:0,cache_read_tokens:0,
            cache_write_tokens:0,reasoning_tokens:0,estimated_cost_usd:0,api_calls:0};
          for(const k of Object.keys(S.usageStats)){
            S.usageStats[k]=(S.usageStats[k]||0)+(d.usage[k]||0);
          }
        }
        clearLiveToolCards();
        S.busy=false;
        syncTopbar();renderMessages();loadDir('.');
      }
      renderSessionList();setBusy(false);setStatus('');
    });

    source.addEventListener('error',e=>{
      source.close();
      // Attempt one reconnect if the stream is still active server-side
      if(!_reconnectAttempted && streamId){
        _reconnectAttempted=true;
        setStatus('Connection lost \u2014 reconnecting\u2026');
        setTimeout(async()=>{
          try{
            const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
            if(st.active){
              setStatus('Reconnected');
              _wireSSE(new EventSource(`/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`));
              return;
            }
          }catch(_){}
          _handleStreamError();
        },1500);
        return;
      }
      _handleStreamError();
    });

    source.addEventListener('cancel',e=>{
      source.close();
      delete INFLIGHT[activeSid];clearInflight();stopApprovalPolling();
      if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard();
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;const _cbc=$('btnCancel');if(_cbc)_cbc.style.display='none';
      }
      if(S.session&&S.session.session_id===activeSid){
        clearLiveToolCards();if(!assistantText)removeThinking();
        S.messages.push({role:'assistant',content:'*Task cancelled.*'});renderMessages();
      }
      renderSessionList();
      if(!S.session||!INFLIGHT[S.session.session_id]){setBusy(false);setStatus('');}
    });
  }

  function _handleStreamError(){
    delete INFLIGHT[activeSid];clearInflight();stopApprovalPolling();
    if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard();
    if(S.session&&S.session.session_id===activeSid){
      S.activeStreamId=null;const _cbe=$('btnCancel');if(_cbe)_cbe.style.display='none';
      clearLiveToolCards();if(!assistantText)removeThinking();
      S.messages.push({role:'assistant',content:'**Error:** Connection lost'});renderMessages();
    }else{
      // User switched away — show background error banner
      if(typeof trackBackgroundError==='function'){
        // Look up session title from the session list cache so the banner names it correctly
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        trackBackgroundError(activeSid,_errTitle,'Connection lost');
      }
    }
    if(!S.session||!INFLIGHT[S.session.session_id]){setBusy(false);setStatus('Error: Connection lost');}
  }

  _wireSSE(new EventSource(`/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`));

}

function transcript(){
  const lines=[`# Hermes session ${S.session?.session_id||''}`,``,
    `Workspace: ${S.session?.workspace||''}`,`Model: ${S.session?.model||''}`,``];
  for(const m of S.messages){
    if(!m||m.role==='tool')continue;
    let c=m.content||'';
    if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('\n');
    const ct=String(c).trim();
    if(!ct&&!m.attachments?.length)continue;
    const attach=m.attachments?.length?`\n\n_Files: ${m.attachments.join(', ')}_`:'';
    lines.push(`## ${m.role}`,'',ct+attach,'');
  }
  return lines.join('\n');
}

function autoResize(){const el=$('msg');el.style.height='auto';el.style.height=Math.min(el.scrollHeight,200)+'px';}


// ── Approval polling ──
let _approvalPollTimer = null;

// showApprovalCard moved above respondApproval

function hideApprovalCard() {
  $("approvalCard").classList.remove("visible");
  $("approvalCmd").textContent = "";
  $("approvalDesc").textContent = "";
}

// Track session_id of the active approval so respond goes to the right session
let _approvalSessionId = null;

function showApprovalCard(pending) {
  $("approvalDesc").textContent = pending.description || "";
  $("approvalCmd").textContent = pending.command || "";
  const keys = pending.pattern_keys || (pending.pattern_key ? [pending.pattern_key] : []);
  $("approvalDesc").textContent = (pending.description || "") + (keys.length ? " [" + keys.join(", ") + "]" : "");
  _approvalSessionId = pending._session_id || (S.session && S.session.session_id) || null;
  $("approvalCard").classList.add("visible");
}

async function respondApproval(choice) {
  const sid = _approvalSessionId || (S.session && S.session.session_id);
  if (!sid) return;
  hideApprovalCard();
  _approvalSessionId = null;
  try {
    await api("/api/approval/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: sid, choice })
    });
  } catch(e) { setStatus("Approval error: " + e.message); }
}

function startApprovalPolling(sid) {
  stopApprovalPolling();
  _approvalPollTimer = setInterval(async () => {
    if (!S.busy || !S.session || S.session.session_id !== sid) {
      stopApprovalPolling(); hideApprovalCard(); return;
    }
    try {
      const data = await api("/api/approval/pending?session_id=" + encodeURIComponent(sid));
      if (data.pending) { data.pending._session_id=sid; showApprovalCard(data.pending); }
      else { hideApprovalCard(); }
    } catch(e) { /* ignore poll errors */ }
  }, 1500);
}

function stopApprovalPolling() {
  if (_approvalPollTimer) { clearInterval(_approvalPollTimer); _approvalPollTimer = null; }
}
// ── Panel navigation (Chat / Tasks / Skills / Memory) ──

