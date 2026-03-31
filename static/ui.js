const S={session:null,messages:[],entries:[],busy:false,pendingFiles:[],toolCalls:[]};
const INFLIGHT={};  // keyed by session_id while request in-flight
const MSG_QUEUE=[];  // messages queued while a request is in-flight
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function renderMd(raw){
  let s=raw||'';
  s=s.replace(/```([\w+-]*)\n?([\s\S]*?)```/g,(_,lang,code)=>{const h=lang?`<div class="pre-header">${esc(lang)}</div>`:'';return `${h}<pre><code>${esc(code.replace(/\n$/,''))}</code></pre>`;});
  s=s.replace(/`([^`\n]+)`/g,(_,c)=>`<code>${esc(c)}</code>`);
  s=s.replace(/\*\*\*(.+?)\*\*\*/g,'<strong><em>$1</em></strong>');
  s=s.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/\*([^*\n]+)\*/g,'<em>$1</em>');
  s=s.replace(/^### (.+)$/gm,'<h3>$1</h3>').replace(/^## (.+)$/gm,'<h2>$1</h2>').replace(/^# (.+)$/gm,'<h1>$1</h1>');
  s=s.replace(/^---+$/gm,'<hr>');
  s=s.replace(/^> (.+)$/gm,'<blockquote>$1</blockquote>');
  // B8: improved list handling supporting up to 2 levels of indentation
  s=s.replace(/((?:^(?:  )?[-*+] .+\n?)+)/gm,block=>{
    const lines=block.trimEnd().split('\n');
    let html='<ul>';
    for(const l of lines){
      const indent=/^ {2,}/.test(l);
      const text=l.replace(/^ {0,4}[-*+] /,'');
      if(indent) html+=`<li style="margin-left:16px">${text}</li>`;
      else html+=`<li>${text}</li>`;
    }
    return html+'</ul>';
  });
  s=s.replace(/((?:^(?:  )?\d+\. .+\n?)+)/gm,block=>{
    const lines=block.trimEnd().split('\n');
    let html='<ol>';
    for(const l of lines){
      const text=l.replace(/^ {0,4}\d+\. /,'');
      html+=`<li>${text}</li>`;
    }
    return html+'</ol>';
  });
  s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  // Tables: | col | col | header row followed by | --- | --- | separator then data rows
  s=s.replace(/((?:^\|.+\|\n?)+)/gm,block=>{
    const rows=block.trim().split('\n').filter(r=>r.trim());
    if(rows.length<2)return block;
    const isSep=r=>/^\|[\s|:-]+\|$/.test(r.trim());
    if(!isSep(rows[1]))return block;
    const parseRow=r=>r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<td>${c.trim()}</td>`).join('');
    const parseHeader=r=>r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<th>${c.trim()}</th>`).join('');
    const header=`<tr>${parseHeader(rows[0])}</tr>`;
    const body=rows.slice(2).map(r=>`<tr>${parseRow(r)}</tr>`).join('');
    return `<table><thead>${header}</thead><tbody>${body}</tbody></table>`;
  });
  const parts=s.split(/\n{2,}/);
  s=parts.map(p=>{p=p.trim();if(!p)return '';if(/^<(h[1-6]|ul|ol|pre|hr|blockquote)/.test(p))return p;return `<p>${p.replace(/\n/g,'<br>')}</p>`;}).join('\n');
  return s;
}

function setStatus(t){
  const bar=$('activityBar');
  const txt=$('activityText');
  if(!bar||!txt)return;
  if(!t){
    bar.style.display='none';
    txt.textContent='';
  } else {
    txt.textContent=t;
    bar.style.display='';
  }
}
function setBusy(v){
  S.busy=v;
  $('btnSend').disabled=v;
  const dots=$('activityDots');
  if(dots) dots.style.display=v?'flex':'none';
  if(!v){
    setStatus('');
    updateQueueBadge();
    // Drain one queued message after UI settles
    if(MSG_QUEUE.length>0){
      const next=MSG_QUEUE.shift();
      updateQueueBadge();
      setTimeout(()=>{ $('msg').value=next; send(); }, 120);
    }
  }
}

function updateQueueBadge(){
  let badge=$('queueBadge');
  if(MSG_QUEUE.length>0){
    if(!badge){
      badge=document.createElement('div');
      badge.id='queueBadge';
      badge.style.cssText='position:fixed;bottom:80px;right:24px;background:rgba(124,185,255,.18);border:1px solid rgba(124,185,255,.4);color:var(--blue);font-size:12px;font-weight:600;padding:6px 14px;border-radius:20px;z-index:50;pointer-events:none;backdrop-filter:blur(8px);';
      document.body.appendChild(badge);
    }
    badge.textContent=MSG_QUEUE.length===1?'1 message queued':`${MSG_QUEUE.length} messages queued`;
  } else {
    if(badge) badge.remove();
  }
}
function showToast(msg,ms){const el=$('toast');el.textContent=msg;el.classList.add('show');clearTimeout(el._t);el._t=setTimeout(()=>el.classList.remove('show'),ms||2800);}

function copyMsg(btn){
  const row=btn.closest('.msg-row');
  const text=row?row.dataset.rawText:'';
  if(!text)return;
  navigator.clipboard.writeText(text).then(()=>{
    const orig=btn.innerHTML;btn.innerHTML='&#10003;';btn.style.color='var(--blue)';
    setTimeout(()=>{btn.innerHTML=orig;btn.style.color='';},1500);
  }).catch(()=>showToast('Copy failed'));
}

// ── Reconnect banner (B4/B5: reload resilience) ──
const INFLIGHT_KEY = 'hermes-webui-inflight'; // localStorage key for in-flight session tracking

function markInflight(sid, streamId) {
  localStorage.setItem(INFLIGHT_KEY, JSON.stringify({sid, streamId, ts: Date.now()}));
}
function clearInflight() {
  localStorage.removeItem(INFLIGHT_KEY);
}
function showReconnectBanner(msg) {
  $('reconnectMsg').textContent = msg || 'A response may have been in progress when you last left.';
  $('reconnectBanner').classList.add('visible');
}
function dismissReconnect() {
  $('reconnectBanner').classList.remove('visible');
  clearInflight();
}
async function refreshSession() {
  dismissReconnect();
  if (!S.session) return;
  try {
    const data = await api(`/api/session?session_id=${encodeURIComponent(S.session.session_id)}`);
    S.session = data.session;
    S.messages = (data.session.messages || []).filter(m => {
      if (!m || !m.role || m.role === 'tool') return false;
      if (m.role === 'assistant') { let c = m.content || ''; if (Array.isArray(c)) c = c.map(p => p.text||'').join(''); return String(c).trim().length > 0; }
      return true;
    });
    syncTopbar(); renderMessages();
    showToast('Conversation refreshed');
  } catch(e) { setStatus('Refresh failed: ' + e.message); }
}
async function checkInflightOnBoot(sid) {
  const raw = localStorage.getItem(INFLIGHT_KEY);
  if (!raw) return;
  try {
    const {sid: inflightSid, streamId, ts} = JSON.parse(raw);
    if (inflightSid !== sid) { clearInflight(); return; }
    // Only show banner if the in-flight entry is less than 10 minutes old
    if (Date.now() - ts > 10 * 60 * 1000) { clearInflight(); return; }
    // Check if stream is still active
    const status = await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId || '')}`);
    if (status.active) {
      // Stream is genuinely still running -- show the banner
      showReconnectBanner('A response is still being generated. Reload when ready?');
    } else {
      // Stream finished. Only show banner if reload happened within 90 seconds
      // (longer gap = normal completed session, not a mid-stream reload)
      if (Date.now() - ts < 90 * 1000) {
        showReconnectBanner('A response was in progress when you last left. Messages may have updated.');
      } else {
        clearInflight();  // completed normally, no banner needed
      }
    }
  } catch(e) { clearInflight(); }
}

function syncTopbar(){
  if(!S.session){
    // Show default workspace name even without a session
    const sidebarName=$('sidebarWsName');
    if(sidebarName && sidebarName.textContent==='Workspace'){
      sidebarName.textContent='No workspace';
    }
    return;
  }
  $('topbarTitle').textContent=S.session.title||'Untitled';
  const vis=S.messages.filter(m=>m&&m.role&&m.role!=='tool');
  $('topbarMeta').textContent=`${vis.length} messages`;
  const m=S.session.model||'';
  const MODEL_LABELS={'openai/gpt-5.4-mini':'GPT-5.4 Mini','openai/gpt-4o':'GPT-4o','openai/o3':'o3','openai/o4-mini':'o4-mini','anthropic/claude-sonnet-4.6':'Sonnet 4.6','anthropic/claude-sonnet-4-5':'Sonnet 4.5','anthropic/claude-haiku-3-5':'Haiku 3.5','google/gemini-2.5-pro':'Gemini 2.5 Pro','deepseek/deepseek-chat-v3-0324':'DeepSeek V3','meta-llama/llama-4-scout':'Llama 4 Scout'};
  $('modelSelect').value=m;  // set dropdown first so chip reads consistent value
  // Show Clear button only when session has messages
  const clearBtn=$('btnClearConv');
  if(clearBtn) clearBtn.style.display=(S.messages&&S.messages.filter(m=>m.role!=='tool').length>0)?'':'none';
  const displayModel=$('modelSelect').value||m;
  $('modelChip').textContent=MODEL_LABELS[displayModel]||(displayModel.split('/').pop()||'Unknown');
  const ws=S.session.workspace||'';
  $('wsChip').textContent=ws.split('/').slice(-2).join('/')||ws;
  // Update workspace chip in topbar with friendly name from workspace list
  const wsChipEl=$('wsChip');
  if(wsChipEl){
    const wsFriendly=getWorkspaceFriendlyName(ws);
    wsChipEl.textContent='\u{1F4C1} '+wsFriendly+' \u25BE';
  }
  // Update sidebar workspace display
  const sidebarName=$('sidebarWsName');
  const sidebarPath=$('sidebarWsPath');
  if(sidebarName){
    sidebarName.textContent=getWorkspaceFriendlyName(ws);
  }
  if(sidebarPath){
    sidebarPath.textContent=ws;
  }
  // modelSelect already set above
}

function msgContent(m){
  // Extract plain text content from a message for filtering
  let c=m.content||'';
  if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('').trim();
  return String(c).trim();
}

function renderMessages(){
  const inner=$('msgInner');
  const vis=S.messages.filter(m=>{
    if(!m||!m.role||m.role==='tool')return false;
    return msgContent(m)||m.attachments?.length;
  });
  $('emptyState').style.display=vis.length?'none':'';
  inner.innerHTML='';
  // Track original indices (in S.messages) so truncate knows the cut point
  const visWithIdx=[];
  let rawIdx=0;
  for(const m of S.messages){
    if(!m||!m.role||m.role==='tool'){rawIdx++;continue;}
    if(msgContent(m)||m.attachments?.length) visWithIdx.push({m,rawIdx});
    rawIdx++;
  }
  for(let vi=0;vi<visWithIdx.length;vi++){
    const {m,rawIdx}=visWithIdx[vi];
    let content=m.content||'';
    if(Array.isArray(content))content=content.filter(p=>p&&p.type==='text').map(p=>p.text||p.content||'').join('\n');
    const isUser=m.role==='user';
    const isLastAssistant=!isUser&&vi===visWithIdx.length-1;
    const row=document.createElement('div');row.className='msg-row';
    row.dataset.msgIdx=rawIdx;
    let filesHtml='';
    if(m.attachments&&m.attachments.length)
      filesHtml=`<div class="msg-files">${m.attachments.map(f=>`<div class="msg-file-badge">&#128206; ${esc(f)}</div>`).join('')}</div>`;
    const bodyHtml = isUser ? esc(String(content)).replace(/\n/g,'<br>') : renderMd(String(content));
    // Action buttons for this bubble
    const editBtn  = isUser  ? `<button class="msg-action-btn" title="Edit message" onclick="editMessage(this)">&#9998;</button>` : '';
    const retryBtn = isLastAssistant ? `<button class="msg-action-btn" title="Regenerate response" onclick="regenerateResponse(this)">&#8635;</button>` : '';
    row.innerHTML=`<div class="msg-role ${m.role}"><div class="role-icon ${m.role}">${isUser?'Y':'H'}</div><span style="font-size:12px">${isUser?'You':'Hermes'}</span><span class="msg-actions">${editBtn}<button class="msg-copy-btn msg-action-btn" title="Copy" onclick="copyMsg(this)">&#128203;</button>${retryBtn}</span></div>${filesHtml}<div class="msg-body">${bodyHtml}</div>`;
    row.dataset.rawText = String(content).trim();
    inner.appendChild(row);
  }
  // Insert tool call cards between last user message and last assistant response
  if(S.toolCalls && S.toolCalls.length){
    // Find the position to insert: after the last user message row
    const allRows = Array.from(inner.querySelectorAll('.msg-row'));
    let insertBefore = null;
    for(let i = allRows.length-1; i >= 0; i--){
      const idx = parseInt(allRows[i].dataset.msgIdx||'-1',10);
      if(idx >= 0 && S.messages[idx] && S.messages[idx].role === 'assistant'){
        insertBefore = allRows[i]; // insert tool cards before last assistant row
      } else if(insertBefore){
        break;
      }
    }
    // Remove any existing tool-card rows to avoid duplicates
    inner.querySelectorAll('.tool-card-row').forEach(el=>el.remove());
    // Render tool cards
    const cardsFragment = document.createDocumentFragment();
    for(const tc of S.toolCalls){
      const row = document.createElement('div');
      row.className = 'msg-row tool-card-row';
      const icon = toolIcon(tc.name);
      const hasDetail = tc.snippet || (tc.args && Object.keys(tc.args).length > 0);
      // Smarter snippet truncation at sentence boundary
      let displaySnippet = '';
      if(tc.snippet){
        const s = tc.snippet;
        if(s.length <= 220){
          displaySnippet = s;
        } else {
          // Find last sentence break within 220 chars
          const cutoff = s.slice(0, 220);
          const lastBreak = Math.max(
            cutoff.lastIndexOf('. '), cutoff.lastIndexOf('\n'),
            cutoff.lastIndexOf('; ')
          );
          displaySnippet = lastBreak > 80 ? s.slice(0, lastBreak + 1) : cutoff;
        }
      }
      const hasMore = tc.snippet && tc.snippet.length > displaySnippet.length;
      const runIndicator = tc.done===false
        ? '<span class="tool-card-running-dot"></span>'
        : '';
      row.innerHTML = `
        <div class="tool-card${tc.done===false?' tool-card-running':''}">
          <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
            ${runIndicator}
            <span class="tool-card-icon">${icon}</span>
            <span class="tool-card-name">${esc(tc.name)}</span>
            <span class="tool-card-preview">${esc(tc.preview||displaySnippet||'')}</span>
            ${hasDetail?'<span class="tool-card-toggle">▸</span>':''}
          </div>
          ${hasDetail?`<div class="tool-card-detail">
            ${tc.args&&Object.keys(tc.args).length?`<div class="tool-card-args">${
              Object.entries(tc.args).map(([k,v])=>`<div><span class="tool-arg-key">${esc(k)}</span> <span class="tool-arg-val">${esc(String(v))}</span></div>`).join('')
            }</div>`:''}
            ${displaySnippet?`<div class="tool-card-result">
              <pre>${esc(displaySnippet)}</pre>
              ${hasMore?`<button class="tool-card-more" onclick="event.stopPropagation();const p=this.previousElementSibling;const full=${JSON.stringify(tc.snippet||'')};p.textContent=p.textContent.length<full.length?full:${JSON.stringify(displaySnippet||'')};this.textContent=p.textContent.length<full.length?'Show more':'Show less'">Show more</button>`:''}
            </div>`:''}
          </div>`:''}
        </div>`;
      cardsFragment.appendChild(row);
    }
    if(insertBefore){
      inner.insertBefore(cardsFragment, insertBefore);
    } else {
      inner.appendChild(cardsFragment);
    }
  }
  $('messages').scrollTop=$('messages').scrollHeight;
  // Apply syntax highlighting after DOM is built
  requestAnimationFrame(()=>highlightCode());
  // Refresh todo panel if it's currently open
  if(typeof loadTodos==='function' && document.getElementById('panelTodos') && document.getElementById('panelTodos').classList.contains('active')){
    loadTodos();
  }
}

function toolIcon(name){
  const icons={terminal:'⬛',read_file:'📄',write_file:'✏️',search_files:'🔍',
    web_search:'🌐',web_extract:'🌐',execute_code:'⚙️',patch:'🔧',
    memory:'🧠',skill_manage:'📚',todo:'✅',cronjob:'⏱️',delegate_task:'🤖',
    send_message:'💬',browser_navigate:'🌐',vision_analyze:'👁️'};
  return icons[name]||'🔧';
}

// ── Edit + Regenerate ──

function editMessage(btn) {
  if(S.busy) return;
  const row = btn.closest('.msg-row');
  if(!row) return;
  const msgIdx = parseInt(row.dataset.msgIdx, 10);
  const originalText = row.dataset.rawText || '';
  const body = row.querySelector('.msg-body');
  if(!body || row.dataset.editing) return;
  row.dataset.editing = '1';

  // Replace msg-body with an editable textarea
  const ta = document.createElement('textarea');
  ta.className = 'msg-edit-area';
  ta.value = originalText;
  body.replaceWith(ta);
  // Resize after DOM insertion so scrollHeight is correct
  requestAnimationFrame(() => { autoResizeTextarea(ta); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); });
  ta.addEventListener('input', () => autoResizeTextarea(ta));

  // Action bar below the textarea
  const bar = document.createElement('div');
  bar.className = 'msg-edit-bar';
  bar.innerHTML = `<button class="msg-edit-send">Send edit</button><button class="msg-edit-cancel">Cancel</button>`;
  ta.after(bar);

  bar.querySelector('.msg-edit-send').onclick = async () => {
    const newText = ta.value.trim();
    if(!newText) return;
    await submitEdit(msgIdx, newText);
  };
  bar.querySelector('.msg-edit-cancel').onclick = () => cancelEdit(row, originalText, body);

  ta.addEventListener('keydown', e => {
    if(e.key==='Enter' && !e.shiftKey) { e.preventDefault(); bar.querySelector('.msg-edit-send').click(); }
    if(e.key==='Escape') { e.preventDefault(); cancelEdit(row, originalText, body); }
  });
}

function cancelEdit(row, originalText, originalBody) {
  delete row.dataset.editing;
  const ta = row.querySelector('.msg-edit-area');
  const bar = row.querySelector('.msg-edit-bar');
  if(ta) ta.replaceWith(originalBody);
  if(bar) bar.remove();
}

function autoResizeTextarea(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 300) + 'px';
}

async function submitEdit(msgIdx, newText) {
  if(!S.session || S.busy) return;
  // Truncate session at msgIdx (keep messages before the edited one)
  // then re-send the edited text
  try {
    await api('/api/session/truncate', {method:'POST', body:JSON.stringify({
      session_id: S.session.session_id,
      keep_count: msgIdx  // keep messages[0..msgIdx-1], discard from msgIdx onward
    })});
    S.messages = S.messages.slice(0, msgIdx);
    renderMessages();
    // Now send the edited message as a new chat
    $('msg').value = newText;
    await send();
  } catch(e) { setStatus('Edit failed: ' + e.message); }
}

async function regenerateResponse(btn) {
  if(!S.session || S.busy) return;
  // Find the last user message and re-run it
  // Remove the last assistant message first (truncate to before it)
  const row = btn.closest('.msg-row');
  if(!row) return;
  const assistantIdx = parseInt(row.dataset.msgIdx, 10);
  // Find the last user message text (one before this assistant message)
  let lastUserText = '';
  for(let i = assistantIdx - 1; i >= 0; i--) {
    const m = S.messages[i];
    if(m && m.role === 'user') { lastUserText = msgContent(m); break; }
  }
  if(!lastUserText) return;
  try {
    await api('/api/session/truncate', {method:'POST', body:JSON.stringify({
      session_id: S.session.session_id,
      keep_count: assistantIdx  // remove the assistant message
    })});
    S.messages = S.messages.slice(0, assistantIdx);
    renderMessages();
    $('msg').value = lastUserText;
    await send();
  } catch(e) { setStatus('Regenerate failed: ' + e.message); }
}

function highlightCode(container) {
  // Apply Prism.js syntax highlighting to all code blocks in container (or whole messages area)
  if(typeof Prism === 'undefined' || !Prism.highlightAllUnder) return;
  const el = container || $('msgInner');
  if(!el) return;
  // Prism autoloader handles language detection via class="language-xxx"
  Prism.highlightAllUnder(el);
}

function appendThinking(){
  $('emptyState').style.display='none';
  const row=document.createElement('div');row.className='msg-row';row.id='thinkingRow';
  row.innerHTML=`<div class="msg-role assistant"><div class="role-icon assistant">H</div>Hermes</div><div class="thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>`;
  $('msgInner').appendChild(row);$('messages').scrollTop=$('messages').scrollHeight;
}
function removeThinking(){const el=$('thinkingRow');if(el)el.remove();}

function fileIcon(name, type){
  if(type==='dir') return '📁';
  const e=fileExt(name);
  if(IMAGE_EXTS.has(e)) return '📷';
  if(MD_EXTS.has(e))    return '📝';
  if(e==='.py')   return '🐍';
  if(e==='.js'||e==='.ts'||e==='.jsx'||e==='.tsx') return '⚡';
  if(e==='.json'||e==='.yaml'||e==='.yml'||e==='.toml') return '⚙';
  if(e==='.sh'||e==='.bash') return '💻';
  return '📄';
}

function renderFileTree(){
  const box=$('fileTree');box.innerHTML='';
  for(const item of S.entries){
    const el=document.createElement('div');el.className='file-item';

    // Icon
    const iconEl=document.createElement('span');
    iconEl.className='file-icon';iconEl.textContent=fileIcon(item.name,item.type);
    el.appendChild(iconEl);

    // Name -- takes all remaining space, truncates with ellipsis
    const nameEl=document.createElement('span');
    nameEl.className='file-name';nameEl.textContent=item.name;nameEl.title=item.name;
    el.appendChild(nameEl);

    // Size -- only for files, right-aligned, shrinks but never wraps
    if(item.type==='file'&&item.size){
      const sizeEl=document.createElement('span');
      sizeEl.className='file-size';
      sizeEl.textContent=`${(item.size/1024).toFixed(1)}k`;
      el.appendChild(sizeEl);
    }

    // Delete button -- only for files, shown as a CSS class toggle on hover
    if(item.type==='file'){
      const del=document.createElement('button');
      del.className='file-del-btn';del.title='Delete';del.textContent='×';
      del.onclick=async(e)=>{e.stopPropagation();await deleteWorkspaceFile(item.path,item.name);};
      el.appendChild(del);
    }

    el.onclick=async()=>item.type==='dir'?loadDir(item.path):openFile(item.path);
    box.appendChild(el);
  }
}

async function deleteWorkspaceFile(relPath, name){
  if(!S.session)return;
  if(!confirm(`Delete ${name}?`))return;
  try{
    await api('/api/file/delete',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath})});
    showToast(`Deleted ${name}`);
    // Close preview if we just deleted the viewed file
    if($('previewPathText').textContent===relPath)$('btnClearPreview').onclick();
    await loadDir('.');
  }catch(e){setStatus('Delete failed: '+e.message);}
}

async function promptNewFile(){
  if(!S.session)return;
  const name=prompt('New file name (e.g. notes.md):','');
  if(!name||!name.trim())return;
  try{
    await api('/api/file/create',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:name.trim(),content:''})});
    showToast(`Created ${name.trim()}`);
    await loadDir('.');
    // Open the new file immediately
    openFile(name.trim());
  }catch(e){setStatus('Create failed: '+e.message);}
}

function renderTray(){
  const tray=$('attachTray');tray.innerHTML='';
  if(!S.pendingFiles.length){tray.classList.remove('has-files');return;}
  tray.classList.add('has-files');
  S.pendingFiles.forEach((f,i)=>{
    const chip=document.createElement('div');chip.className='attach-chip';
    chip.innerHTML=`&#128206; ${esc(f.name)} <button title="Remove">&#10005;</button>`;
    chip.querySelector('button').onclick=()=>{S.pendingFiles.splice(i,1);renderTray();};
    tray.appendChild(chip);
  });
}
function addFiles(files){for(const f of files){if(!S.pendingFiles.find(p=>p.name===f.name))S.pendingFiles.push(f);}renderTray();}

async function uploadPendingFiles(){
  if(!S.pendingFiles.length||!S.session)return[];
  const names=[];let failures=0;
  const bar=$('uploadBar');const barWrap=$('uploadBarWrap');
  barWrap.classList.add('active');bar.style.width='0%';
  const total=S.pendingFiles.length;
  for(let i=0;i<total;i++){
    const f=S.pendingFiles[i];const fd=new FormData();
    fd.append('session_id',S.session.session_id);fd.append('file',f,f.name);
    try{
      const res=await fetch('/api/upload',{method:'POST',body:fd});
      if(!res.ok){const err=await res.text();throw new Error(err);}
      const data=await res.json();
      if(data.error)throw new Error(data.error);
      names.push(data.filename);
    }catch(e){failures++;setStatus(`\u274c Upload failed: ${f.name} \u2014 ${e.message}`);}
    bar.style.width=`${Math.round((i+1)/total*100)}%`;
  }
  barWrap.classList.remove('active');bar.style.width='0%';
  S.pendingFiles=[];renderTray();
  if(failures===total&&total>0)throw new Error(`All ${total} upload(s) failed`);
  return names;
}

