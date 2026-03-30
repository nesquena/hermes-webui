const S={session:null,messages:[],entries:[],busy:false,pendingFiles:[]};
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
    row.innerHTML=`<div class="msg-role ${m.role}"><div class="role-icon ${m.role}">${isUser?'You':'H'}</div><span style="font-size:12px">${isUser?'You':'Hermes'}</span><span class="msg-actions">${editBtn}<button class="msg-copy-btn msg-action-btn" title="Copy" onclick="copyMsg(this)">&#128203;</button>${retryBtn}</span></div>${filesHtml}<div class="msg-body">${bodyHtml}</div>`;
    row.dataset.rawText = String(content).trim();
    inner.appendChild(row);
  }
  $('messages').scrollTop=$('messages').scrollHeight;
  // Apply syntax highlighting after DOM is built
  requestAnimationFrame(()=>highlightCode());
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
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  autoResizeTextarea(ta);
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

async function api(path,opts={}){
  const res=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});
  if(!res.ok)throw new Error(await res.text());
  const ct=res.headers.get('content-type')||'';
  return ct.includes('application/json')?res.json():res.text();
}

async function loadDir(path){
  if(!S.session)return;
  try{
    const data=await api(`/api/list?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`);
    S.entries=data.entries||[];renderFileTree();
  }catch(e){console.warn('loadDir',e);}
}

// File extension sets for preview routing (must match server-side sets)
const IMAGE_EXTS  = new Set(['.png','.jpg','.jpeg','.gif','.svg','.webp','.ico','.bmp']);
const MD_EXTS     = new Set(['.md','.markdown','.mdown']);

function fileExt(p){ const i=p.lastIndexOf('.'); return i>=0?p.slice(i).toLowerCase():''; }

let _previewCurrentPath = '';  // relative path of currently previewed file
let _previewCurrentMode = '';  // 'code' | 'md' | 'image'
let _previewDirty = false;     // true when edits are unsaved

function showPreview(mode){
  // mode: 'code' | 'image' | 'md'
  $('previewCode').style.display     = mode==='code'  ? '' : 'none';
  $('previewImgWrap').style.display  = mode==='image' ? '' : 'none';
  $('previewMd').style.display       = mode==='md'    ? '' : 'none';
  $('previewEditArea').style.display = 'none';  // start in read-only
  const badge=$('previewBadge');
  badge.className='preview-badge '+mode;
  badge.textContent = mode==='image'?'image':mode==='md'?'md':fileExt($('previewPathText').textContent)||'text';
  _previewCurrentMode = mode;
  _previewDirty = false;
  updateEditBtn();
}

function updateEditBtn(){
  const btn=$('btnEditFile');
  if(!btn)return;
  const editable = _previewCurrentMode==='code'||_previewCurrentMode==='md';
  btn.style.display = editable?'':'none';
  const editing = $('previewEditArea').style.display!=='none';
  btn.innerHTML = editing ? '&#128190; Save' : '&#9998; Edit';
  btn.title = editing ? 'Save changes' : 'Edit this file';
  btn.style.color = editing ? 'var(--blue)' : '';
  if(_previewDirty) btn.innerHTML = '&#128190; Save*';
}

async function toggleEditMode(){
  const editing = $('previewEditArea').style.display!=='none';
  if(editing){
    // Save
    if(!S.session||!_previewCurrentPath)return;
    const content=$('previewEditArea').value;
    try{
      await api('/api/file/save',{method:'POST',body:JSON.stringify({
        session_id:S.session.session_id, path:_previewCurrentPath, content
      })});
      _previewDirty=false;
      // Update read-only views
      if(_previewCurrentMode==='code') $('previewCode').textContent=content;
      else $('previewMd').innerHTML=renderMd(content);
      $('previewEditArea').style.display='none';
      if(_previewCurrentMode==='code') $('previewCode').style.display='';
      else $('previewMd').style.display='';
      showToast('Saved');
    }catch(e){setStatus('Save failed: '+e.message);}
  }else{
    // Enter edit mode: populate textarea with current content
    const currentText = _previewCurrentMode==='code'
      ? $('previewCode').textContent
      : _previewRawContent||'';
    $('previewEditArea').value=currentText;
    $('previewEditArea').style.display='';
    if(_previewCurrentMode==='code') $('previewCode').style.display='none';
    else $('previewMd').style.display='none';
    // Escape cancels the edit without saving
    $('previewEditArea').onkeydown=e=>{
      if(e.key==='Escape'){e.preventDefault();cancelEditMode();}
    };
  }
  updateEditBtn();
}

let _previewRawContent = '';  // raw text for md files (to populate editor)

function cancelEditMode(){
  // Discard changes and return to read-only view
  $('previewEditArea').style.display='none';
  $('previewEditArea').onkeydown=null;
  if(_previewCurrentMode==='code') $('previewCode').style.display='';
  else $('previewMd').style.display='';
  _previewDirty=false;
  updateEditBtn();
}

async function openFile(path){
  if(!S.session)return;
  const ext=fileExt(path);
  $('previewPathText').textContent=path;
  $('previewArea').classList.add('visible');
  $('fileTree').style.display='none';

  _previewCurrentPath = path;
  if(IMAGE_EXTS.has(ext)){
    // Image: load via raw endpoint, show as <img>
    showPreview('image');
    const url=`/api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`;
    $('previewImg').alt=path;
    $('previewImg').src=url;
    $('previewImg').onerror=()=>setStatus('Could not load image');
  } else if(MD_EXTS.has(ext)){
    // Markdown: fetch text, render with renderMd, display as formatted HTML
    try{
      const data=await api(`/api/file?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`);
      showPreview('md');
      _previewRawContent = data.content;
      $('previewMd').innerHTML=renderMd(data.content);
    }catch(e){setStatus('Could not open file');}
  } else {
    // Plain code / text
    try{
      const data=await api(`/api/file?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`);
      showPreview('code');
      $('previewCode').textContent=data.content;
    }catch(e){setStatus('Could not open file');}
  }
}

async function newSession(flash){
  MSG_QUEUE.length=0;updateQueueBadge();
  const inheritWs=S.session?S.session.workspace:null;
  const data=await api('/api/session/new',{method:'POST',body:JSON.stringify({model:$('modelSelect').value,workspace:inheritWs})});
  S.session=data.session;S.messages=data.session.messages||[];
  if(flash)S.session._flash=true;
  localStorage.setItem('hermes-webui-session',S.session.session_id);
  syncTopbar();await loadDir('.');renderMessages();
  // don't call renderSessionList here - callers do it when needed
}

async function loadSession(sid){
  stopApprovalPolling();hideApprovalCard();
  const data=await api(`/api/session?session_id=${encodeURIComponent(sid)}`);
  S.session=data.session;
  localStorage.setItem('hermes-webui-session',S.session.session_id);
  // B9: sanitize empty assistant messages that can appear when agent only ran tool calls
  data.session.messages=(data.session.messages||[]).filter(m=>{
    if(!m||!m.role)return false;
    if(m.role==='tool')return false;
    if(m.role==='assistant'){let c=m.content||'';if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('');return String(c).trim().length>0;}
    return true;
  });
  if(INFLIGHT[sid]){
    S.messages=INFLIGHT[sid].messages;
    syncTopbar();await loadDir('.');renderMessages();appendThinking();
    setBusy(true);setStatus('Hermes is thinking\u2026');
  }else{
    MSG_QUEUE.length=0;updateQueueBadge();  // clear queue when switching sessions
    S.messages=data.session.messages||[];
    syncTopbar();await loadDir('.');renderMessages();highlightCode();
  }
}

let _allSessions = [];  // cached for search filter
let _renamingSid = null;  // session_id currently being renamed (blocks list re-renders)

async function renderSessionList(){
  try{
    if(!($('sessionSearch').value||'').trim()) _contentSearchResults = [];
    const data=await api('/api/sessions');
    _allSessions = data.sessions||[];
    renderSessionListFromCache();  // no-ops if rename is in progress
  }catch(e){console.warn('renderSessionList',e);}
}

let _searchDebounceTimer = null;
let _contentSearchResults = [];  // results from /api/sessions/search content scan

function filterSessions(){
  // Immediate client-side title filter (no flicker)
  renderSessionListFromCache();
  // Debounced content search via API for message text
  const q = ($('sessionSearch').value || '').trim();
  clearTimeout(_searchDebounceTimer);
  if (!q) { _contentSearchResults = []; return; }
  _searchDebounceTimer = setTimeout(async () => {
    try {
      const data = await api(`/api/sessions/search?q=${encodeURIComponent(q)}&content=1&depth=5`);
      const titleIds = new Set(_allSessions.filter(s => (s.title||'Untitled').toLowerCase().includes(q.toLowerCase())).map(s=>s.session_id));
      _contentSearchResults = (data.sessions||[]).filter(s => s.match_type === 'content' && !titleIds.has(s.session_id));
      renderSessionListFromCache();
    } catch(e) { /* ignore */ }
  }, 350);
}

function renderSessionListFromCache(){
  // Don't re-render while user is actively renaming a session (would destroy the input)
  if(_renamingSid) return;
  const q=($('sessionSearch').value||'').toLowerCase();
  const titleMatches=q?_allSessions.filter(s=>(s.title||'Untitled').toLowerCase().includes(q)):_allSessions;
  // Merge content matches (deduped): content matches appended after title matches
  const titleIds=new Set(titleMatches.map(s=>s.session_id));
  const sessions=q?[...titleMatches,..._contentSearchResults.filter(s=>!titleIds.has(s.session_id))]:titleMatches;
  const list=$('sessionList');list.innerHTML='';
  // Date grouping: Today / Yesterday / Earlier
  const now=Date.now();
  const ONE_DAY=86400000;
  let lastGroup='';
  for(const s of sessions.slice(0,50)){
    const ts=(s.updated_at||0)*1000;
    const group=ts>now-ONE_DAY?'Today':ts>now-2*ONE_DAY?'Yesterday':'Earlier';
    if(group!==lastGroup){
      lastGroup=group;
      const hdr=document.createElement('div');
      hdr.style.cssText='font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);padding:10px 10px 4px;opacity:.8;';
      hdr.textContent=group;
      list.appendChild(hdr);
    }
    const el=document.createElement('div');
    const isActive=S.session&&s.session_id===S.session.session_id;
    el.className='session-item'+(isActive?' active':'')+(isActive&&S.session&&S.session._flash?' new-flash':'');
    if(isActive&&S.session&&S.session._flash)delete S.session._flash;
    const title=document.createElement('span');
    title.className='session-title';title.textContent=s.title||'Untitled';
    title.title='Double-click to rename';

    // Rename: called directly when we confirm it's a double-click
    const startRename=()=>{
      _renamingSid = s.session_id;
      const inp=document.createElement('input');
      inp.className='session-title-input';
      inp.value=s.title||'Untitled';
      ['click','mousedown','dblclick','pointerdown'].forEach(ev=>
        inp.addEventListener(ev, e2=>e2.stopPropagation())
      );
      const finish=async(save)=>{
        _renamingSid = null;
        if(save){
          const newTitle=inp.value.trim()||'Untitled';
          title.textContent=newTitle;
          s.title=newTitle;
          if(S.session&&S.session.session_id===s.session_id){S.session.title=newTitle;syncTopbar();}
          try{await api('/api/session/rename',{method:'POST',body:JSON.stringify({session_id:s.session_id,title:newTitle})});}
          catch(err){setStatus('Rename failed: '+err.message);}
        }
        inp.replaceWith(title);
        // Allow list re-renders again after a short delay
        setTimeout(()=>{ if(_renamingSid===null) renderSessionListFromCache(); },50);
      };
      inp.onkeydown=e2=>{
        if(e2.key==='Enter'){e2.preventDefault();e2.stopPropagation();finish(true);}
        if(e2.key==='Escape'){e2.preventDefault();e2.stopPropagation();finish(false);}
      };
      // onblur: cancel only -- no accidental saves
      inp.onblur=()=>{ if(_renamingSid===s.session_id) finish(false); };
      title.replaceWith(inp);
      setTimeout(()=>{inp.focus();inp.select();},10);
    };

    const trash=document.createElement('button');
    trash.className='session-trash';trash.innerHTML='&#128465;';trash.title='Delete';
    trash.onclick=async(e)=>{e.stopPropagation();e.preventDefault();await deleteSession(s.session_id);};
    el.appendChild(title);el.appendChild(trash);

    // Use a click timer to distinguish single-click (navigate) from double-click (rename).
    // This prevents loadSession from firing on the first click of a double-click,
    // which would re-render the list and destroy the dblclick target before it fires.
    let _clickTimer=null;
    el.onclick=async(e)=>{
      if(_renamingSid) return; // ignore while any rename is active
      if(e.target===trash||trash.contains(e.target)) return; // trash handles itself
      clearTimeout(_clickTimer);
      _clickTimer=setTimeout(async()=>{
        _clickTimer=null;
        if(_renamingSid) return;
        await loadSession(s.session_id);renderSessionListFromCache();
      }, 220);
    };
    el.ondblclick=async(e)=>{
      e.stopPropagation();
      e.preventDefault();
      clearTimeout(_clickTimer); // cancel the pending single-click navigation
      _clickTimer=null;
      startRename();
    };
    list.appendChild(el);
  }
}

async function deleteSession(sid){
  if(!confirm('Delete this conversation?'))return;
  try{
    await api('/api/session/delete',{method:'POST',body:JSON.stringify({session_id:sid})});
  }catch(e){setStatus(`Delete failed: ${e.message}`);return;}
  if(S.session&&S.session.session_id===sid){
    S.session=null;S.messages=[];S.entries=[];
    localStorage.removeItem('hermes-webui-session');
    // load the most recent remaining session, or show blank if none left
    const remaining=await api('/api/sessions');
    if(remaining.sessions&&remaining.sessions.length){
      await loadSession(remaining.sessions[0].session_id);
    }else{
      $('topbarTitle').textContent='Hermes';
      $('topbarMeta').textContent='Start a new conversation';
      $('msgInner').innerHTML='';
      $('emptyState').style.display='';
      $('fileTree').innerHTML='';
    }
  }
  showToast('Conversation deleted');
  await renderSessionList();
}


async function send(){
  const text=$('msg').value.trim();
  if(!text&&!S.pendingFiles.length)return;
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

  setStatus('Uploading…');
  let uploaded=[];
  try{uploaded=await uploadPendingFiles();}
  catch(e){if(!text){setStatus(`❌ ${e.message}`);return;}}

  let msgText=text;
  if(uploaded.length&&!msgText)msgText=`I've uploaded ${uploaded.length} file(s): ${uploaded.join(', ')}`;
  else if(uploaded.length)msgText=`${text}\n\n[Attached files: ${uploaded.join(', ')}]`;
  if(!msgText){setStatus('Nothing to send');return;}

  $('msg').value='';autoResize();
  const displayText=text||(uploaded.length?`Uploaded: ${uploaded.join(', ')}`:'(file upload)');
  const userMsg={role:'user',content:displayText,attachments:uploaded.length?uploaded:undefined};
  S.messages.push(userMsg);renderMessages();appendThinking();setBusy(true);  // activity bar shown via setBusy
  INFLIGHT[activeSid]={messages:[...S.messages],uploaded};
  startApprovalPolling(activeSid);

  // Start the agent via POST, get a stream_id back
  let streamId;
  try{
    const startData=await api('/api/chat/start',{method:'POST',body:JSON.stringify({
      session_id:activeSid,message:msgText,
      model:$('modelSelect').value,workspace:S.session.workspace
    })});
    streamId=startData.stream_id;
    markInflight(activeSid, streamId);
  }catch(e){
    delete INFLIGHT[activeSid];
    stopApprovalPolling();hideApprovalCard();removeThinking();
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

  const es=new EventSource(`/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`);

  es.addEventListener('token',e=>{
    const d=JSON.parse(e.data);
    assistantText+=d.text;
    ensureAssistantRow();
    assistantBody.innerHTML=renderMd(assistantText);
    $('messages').scrollTop=$('messages').scrollHeight;
  });

  es.addEventListener('tool',e=>{
    const d=JSON.parse(e.data);
    // B10: replace thinking dots with a tool-running indicator (not both at once)
    removeThinking();
    setStatus(`${d.name}${d.preview?' · '+d.preview.slice(0,55):''}`);
    // Show a compact "working" row if no tokens have arrived yet
    if(!assistantRow){
      const toolRow=document.createElement('div');toolRow.id='toolRunningRow';toolRow.className='msg-row';
      const icon=document.createElement('div');icon.className='role-icon assistant';icon.textContent='H';
      const lbl=document.createElement('div');lbl.className='msg-role assistant';lbl.appendChild(icon);lbl.appendChild(document.createTextNode('Hermes'));
      const body=document.createElement('div');body.className='msg-body';body.style.cssText='color:var(--muted);font-size:13px;font-style:italic;';
      body.innerHTML=`&#9881; ${d.name} <span style="opacity:.55;font-size:12px">running…</span>`;
      toolRow.appendChild(lbl);toolRow.appendChild(body);
      // Remove any existing tool row before adding new one
      const old=$('toolRunningRow');if(old)old.remove();
      $('msgInner').appendChild(toolRow);
      $('messages').scrollTop=$('messages').scrollHeight;
    }
  });

  es.addEventListener('approval',e=>{
    const d=JSON.parse(e.data);
    showApprovalCard(d);
  });

  es.addEventListener('done',e=>{
    es.close();
    const d=JSON.parse(e.data);
    delete INFLIGHT[activeSid];
    clearInflight();
    stopApprovalPolling();hideApprovalCard();
    if(S.session&&S.session.session_id===activeSid){
      S.session=d.session;S.messages=d.session.messages||[];
      if(uploaded.length){
        const lastUser=[...S.messages].reverse().find(m=>m.role==='user');
        if(lastUser)lastUser.attachments=uploaded;
      }
      syncTopbar();renderMessages();loadDir('.');
    }
    renderSessionList();setBusy(false);setStatus('');
  });

  es.addEventListener('error',e=>{
    es.close();
    delete INFLIGHT[activeSid];
    clearInflight();
    stopApprovalPolling();hideApprovalCard();
    let msg='Connection error';
    try{const d=JSON.parse(e.data);msg=d.message||msg;}catch(_){}
    if(S.session&&S.session.session_id===activeSid){
      if(!assistantText){removeThinking();}
      S.messages.push({role:'assistant',content:`**Error:** ${msg}`});
      renderMessages();
    }
    setBusy(false);setStatus('Error: '+msg);
  });

  // Handle SSE connection errors (network drop etc)
  es.onerror=()=>{
    if(es.readyState===EventSource.CLOSED){
      delete INFLIGHT[activeSid];
      stopApprovalPolling();hideApprovalCard();
      if(!assistantText&&S.session&&S.session.session_id===activeSid){
        removeThinking();
        S.messages.push({role:'assistant',content:'**Error:** Connection lost'});
        renderMessages();
      }
      setBusy(false);setStatus('');
    }
  };
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

function showApprovalCard(pending) {
  $("approvalDesc").textContent = pending.description || "";
  $("approvalCmd").textContent = pending.command || "";
  // show pattern key(s) for context
  const keys = pending.pattern_keys || (pending.pattern_key ? [pending.pattern_key] : []);
  $("approvalDesc").textContent = (pending.description || "") + (keys.length ? " [" + keys.join(", ") + "]" : "");
  $("approvalCard").classList.add("visible");
}

function hideApprovalCard() {
  $("approvalCard").classList.remove("visible");
  $("approvalCmd").textContent = "";
  $("approvalDesc").textContent = "";
}

async function respondApproval(choice) {
  if (!S.session) return;
  hideApprovalCard();
  try {
    await api("/api/approval/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: S.session.session_id, choice })
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
      if (data.pending) { showApprovalCard(data.pending); }
      else { hideApprovalCard(); }
    } catch(e) { /* ignore poll errors */ }
  }, 1500);
}

function stopApprovalPolling() {
  if (_approvalPollTimer) { clearInterval(_approvalPollTimer); _approvalPollTimer = null; }
}
// ── Panel navigation (Chat / Tasks / Skills / Memory) ──

let _currentPanel = 'chat';
let _skillsData = null; // cached skills list

async function switchPanel(name) {
  _currentPanel = name;
  // Update nav tabs
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.toggle('active', t.dataset.panel === name));
  // Update panel views
  document.querySelectorAll('.panel-view').forEach(p => p.classList.remove('active'));
  const panelEl = $('panel' + name.charAt(0).toUpperCase() + name.slice(1));
  if (panelEl) panelEl.classList.add('active');
  // Lazy-load panel data
  if (name === 'tasks') await loadCrons();
  if (name === 'skills') await loadSkills();
  if (name === 'memory') await loadMemory();
  if (name === 'workspaces') await loadWorkspacesPanel();
}

// ── Cron panel ──
async function loadCrons() {
  const box = $('cronList');
  try {
    const data = await api('/api/crons');
    if (!data.jobs || !data.jobs.length) {
      box.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:12px">No scheduled jobs found.</div>';
      return;
    }
    box.innerHTML = '';
    for (const job of data.jobs) {
      const item = document.createElement('div');
      item.className = 'cron-item';
      item.id = 'cron-' + job.id;
      const statusClass = job.enabled === false ? 'disabled' : job.state === 'paused' ? 'paused' : job.last_status === 'error' ? 'error' : 'active';
      const statusLabel = job.enabled === false ? 'off' : job.state === 'paused' ? 'paused' : job.last_status === 'error' ? 'error' : 'active';
      const nextRun = job.next_run_at ? new Date(job.next_run_at).toLocaleString() : 'N/A';
      const lastRun = job.last_run_at ? new Date(job.last_run_at).toLocaleString() : 'never';
      item.innerHTML = `
        <div class="cron-header" onclick="toggleCron('${job.id}')">
          <span class="cron-name" title="${esc(job.name)}">${esc(job.name)}</span>
          <span class="cron-status ${statusClass}">${statusLabel}</span>
        </div>
        <div class="cron-body" id="cron-body-${job.id}">
          <div class="cron-schedule">&#128337; ${esc(job.schedule_display || job.schedule?.expression || '')} &nbsp;|&nbsp; Next: ${esc(nextRun)} &nbsp;|&nbsp; Last: ${esc(lastRun)}</div>
          <div class="cron-prompt">${esc((job.prompt||'').slice(0,300))}${(job.prompt||'').length>300?'…':''}</div>
          <div class="cron-actions">
            <button class="cron-btn run" onclick="cronRun('${job.id}')">&#9654; Run now</button>
            ${statusLabel==='paused'
              ? `<button class="cron-btn" onclick="cronResume('${job.id}')">&#9654;&#9474; Resume</button>`
              : `<button class="cron-btn pause" onclick="cronPause('${job.id}')">&#9646;&#9646; Pause</button>`}
            <button class="cron-btn" onclick="cronEditOpen('${job.id}',${JSON.stringify(job).replace(/"/g,'&quot;')})">&#9998; Edit</button>
            <button class="cron-btn" style="border-color:rgba(201,168,76,.3);color:var(--accent)" onclick="cronDelete('${job.id}')">&#128465; Delete</button>
          </div>
          <!-- Inline edit form, hidden by default -->
          <div id="cron-edit-${job.id}" style="display:none;margin-top:8px;border-top:1px solid var(--border);padding-top:8px">
            <input id="cron-edit-name-${job.id}" placeholder="Job name" style="width:100%;background:rgba(255,255,255,.05);border:1px solid var(--border2);border-radius:6px;color:var(--text);padding:5px 8px;font-size:12px;outline:none;margin-bottom:5px;box-sizing:border-box">
            <input id="cron-edit-schedule-${job.id}" placeholder="Schedule" style="width:100%;background:rgba(255,255,255,.05);border:1px solid var(--border2);border-radius:6px;color:var(--text);padding:5px 8px;font-size:12px;outline:none;margin-bottom:5px;box-sizing:border-box">
            <textarea id="cron-edit-prompt-${job.id}" rows="3" placeholder="Prompt" style="width:100%;background:rgba(255,255,255,.05);border:1px solid var(--border2);border-radius:6px;color:var(--text);padding:5px 8px;font-size:12px;outline:none;resize:none;font-family:inherit;margin-bottom:5px;box-sizing:border-box"></textarea>
            <div id="cron-edit-err-${job.id}" style="font-size:11px;color:var(--accent);display:none;margin-bottom:5px"></div>
            <div style="display:flex;gap:6px">
              <button class="cron-btn run" style="flex:1" onclick="cronEditSave('${job.id}')">Save</button>
              <button class="cron-btn" style="flex:1" onclick="cronEditClose('${job.id}')">Cancel</button>
            </div>
          </div>
          <div id="cron-output-${job.id}">
            <div class="cron-last-header">Last output</div>
            <div class="cron-last" id="cron-out-text-${job.id}" style="color:var(--muted);font-size:11px">Loading…</div>
          </div>
        </div>`;
      box.appendChild(item);
      // Eagerly load last output for visible items
      loadCronOutput(job.id);
    }
  } catch(e) { box.innerHTML = `<div style="padding:12px;color:var(--accent);font-size:12px">Error: ${esc(e.message)}</div>`; }
}

function toggleCronForm(){
  const form=$('cronCreateForm');
  if(!form)return;
  const open=form.style.display!=='none';
  form.style.display=open?'none':'';
  if(!open){
    $('cronFormName').value='';
    $('cronFormSchedule').value='';
    $('cronFormPrompt').value='';
    $('cronFormDeliver').value='local';
    $('cronFormError').style.display='none';
    $('cronFormName').focus();
  }
}

async function submitCronCreate(){
  const name=$('cronFormName').value.trim();
  const schedule=$('cronFormSchedule').value.trim();
  const prompt=$('cronFormPrompt').value.trim();
  const deliver=$('cronFormDeliver').value;
  const errEl=$('cronFormError');
  errEl.style.display='none';
  if(!schedule){errEl.textContent='Schedule is required (e.g. "0 9 * * *" or "every 1h")';errEl.style.display='';return;}
  if(!prompt){errEl.textContent='Prompt is required';errEl.style.display='';return;}
  try{
    await api('/api/crons/create',{method:'POST',body:JSON.stringify({name:name||undefined,schedule,prompt,deliver})});
    toggleCronForm();
    showToast('Job created ✓');
    await loadCrons();
  }catch(e){
    errEl.textContent='Error: '+e.message;errEl.style.display='';
  }
}

async function loadCronOutput(jobId) {
  try {
    const data = await api(`/api/crons/output?job_id=${encodeURIComponent(jobId)}&limit=1`);
    const el = $('cron-out-text-' + jobId);
    if (!el) return;
    if (!data.outputs || !data.outputs.length) { el.textContent = '(no runs yet)'; return; }
    const out = data.outputs[0];
    // Show filename (timestamp) and trimmed content
    const lines = out.content.split('\n');
    const body = lines.slice(lines.findIndex(l => l.startsWith('## Response')) + 1).join('\n').trim();
    el.textContent = out.filename.replace('.md','') + '\n\n' + (body.slice(0, 600) || '(empty)');
  } catch(e) { /* ignore */ }
}

function toggleCron(id) {
  const body = $('cron-body-' + id);
  if (body) body.classList.toggle('open');
}

async function cronRun(id) {
  try {
    await api('/api/crons/run', {method:'POST', body: JSON.stringify({job_id: id})});
    showToast('Job triggered ✓');
    setTimeout(() => loadCronOutput(id), 5000);
  } catch(e) { showToast('Run failed: ' + e.message, 4000); }
}

async function cronPause(id) {
  try {
    await api('/api/crons/pause', {method:'POST', body: JSON.stringify({job_id: id})});
    showToast('Job paused');
    await loadCrons();
  } catch(e) { showToast('Pause failed: ' + e.message, 4000); }
}

async function cronResume(id) {
  try {
    await api('/api/crons/resume', {method:'POST', body: JSON.stringify({job_id: id})});
    showToast('Job resumed ✓');
    await loadCrons();
  } catch(e) { showToast('Resume failed: ' + e.message, 4000); }
}

function cronEditOpen(id, job) {
  const form = $('cron-edit-' + id);
  if (!form) return;
  $('cron-edit-name-' + id).value = job.name || '';
  $('cron-edit-schedule-' + id).value = job.schedule_display || (job.schedule && job.schedule.expression) || job.schedule || '';
  $('cron-edit-prompt-' + id).value = job.prompt || '';
  const errEl = $('cron-edit-err-' + id);
  if (errEl) errEl.style.display = 'none';
  form.style.display = '';
}

function cronEditClose(id) {
  const form = $('cron-edit-' + id);
  if (form) form.style.display = 'none';
}

async function cronEditSave(id) {
  const name = $('cron-edit-name-' + id).value.trim();
  const schedule = $('cron-edit-schedule-' + id).value.trim();
  const prompt = $('cron-edit-prompt-' + id).value.trim();
  const errEl = $('cron-edit-err-' + id);
  if (!schedule) { errEl.textContent = 'Schedule is required'; errEl.style.display = ''; return; }
  if (!prompt) { errEl.textContent = 'Prompt is required'; errEl.style.display = ''; return; }
  try {
    const updates = {job_id: id, schedule, prompt};
    if (name) updates.name = name;
    await api('/api/crons/update', {method:'POST', body: JSON.stringify(updates)});
    showToast('Job updated ✓');
    await loadCrons();
  } catch(e) { errEl.textContent = 'Error: ' + e.message; errEl.style.display = ''; }
}

async function cronDelete(id) {
  if (!confirm('Delete this cron job? This cannot be undone.')) return;
  try {
    await api('/api/crons/delete', {method:'POST', body: JSON.stringify({job_id: id})});
    showToast('Job deleted');
    await loadCrons();
  } catch(e) { showToast('Delete failed: ' + e.message, 4000); }
}

async function clearConversation() {
  if(!S.session) return;
  if(!confirm('Clear all messages in this conversation? This cannot be undone.')) return;
  try {
    const data = await api('/api/session/clear', {method:'POST',
      body: JSON.stringify({session_id: S.session.session_id})});
    S.session = data.session;
    S.messages = [];
    syncTopbar();
    renderMessages();
    showToast('Conversation cleared');
  } catch(e) { setStatus('Clear failed: ' + e.message); }
}

// ── Skills panel ──
async function loadSkills() {
  if (_skillsData) { renderSkills(_skillsData); return; }
  const box = $('skillsList');
  try {
    const data = await api('/api/skills');
    _skillsData = data.skills || [];
    renderSkills(_skillsData);
  } catch(e) { box.innerHTML = `<div style="padding:12px;color:var(--accent);font-size:12px">Error: ${esc(e.message)}</div>`; }
}

function renderSkills(skills) {
  const query = ($('skillsSearch').value || '').toLowerCase();
  const filtered = query ? skills.filter(s =>
    (s.name||'').toLowerCase().includes(query) ||
    (s.description||'').toLowerCase().includes(query) ||
    (s.category||'').toLowerCase().includes(query)
  ) : skills;
  // Group by category
  const cats = {};
  for (const s of filtered) {
    const cat = s.category || '(general)';
    if (!cats[cat]) cats[cat] = [];
    cats[cat].push(s);
  }
  const box = $('skillsList');
  box.innerHTML = '';
  if (!filtered.length) { box.innerHTML = '<div style="padding:12px;color:var(--muted);font-size:12px">No skills match.</div>'; return; }
  for (const [cat, items] of Object.entries(cats).sort()) {
    const sec = document.createElement('div');
    sec.className = 'skills-category';
    sec.innerHTML = `<div class="skills-cat-header">&#128193; ${esc(cat)} <span style="opacity:.5">(${items.length})</span></div>`;
    for (const skill of items.sort((a,b) => a.name.localeCompare(b.name))) {
      const el = document.createElement('div');
      el.className = 'skill-item';
      el.innerHTML = `<span class="skill-name">${esc(skill.name)}</span><span class="skill-desc">${esc(skill.description||'')}</span>`;
      el.onclick = () => openSkill(skill.name, el);
      sec.appendChild(el);
    }
    box.appendChild(sec);
  }
}

function filterSkills() {
  if (_skillsData) renderSkills(_skillsData);
}

async function openSkill(name, el) {
  // Highlight active skill
  document.querySelectorAll('.skill-item').forEach(e => e.classList.remove('active'));
  if (el) el.classList.add('active');
  try {
    const data = await api(`/api/skills/content?name=${encodeURIComponent(name)}`);
    // Show skill content in right panel preview
    $('previewPathText').textContent = name + '.md';
    $('previewBadge').textContent = 'skill';
    $('previewBadge').className = 'preview-badge md';
    showPreview('md');
    $('previewMd').innerHTML = renderMd(data.content || '(no content)');
    $('previewArea').classList.add('visible');
    $('fileTree').style.display = 'none';
  } catch(e) { setStatus('Could not load skill: ' + e.message); }
}

// ── Skill create/edit form ──
let _editingSkillName = null;

function toggleSkillForm(prefillName, prefillCategory, prefillContent) {
  const form = $('skillCreateForm');
  if (!form) return;
  const open = form.style.display !== 'none';
  if (open) { form.style.display = 'none'; _editingSkillName = null; return; }
  $('skillFormName').value = prefillName || '';
  $('skillFormCategory').value = prefillCategory || '';
  $('skillFormContent').value = prefillContent || '';
  $('skillFormError').style.display = 'none';
  _editingSkillName = prefillName || null;
  form.style.display = '';
  $('skillFormName').focus();
}

async function submitSkillSave() {
  const name = ($('skillFormName').value||'').trim().toLowerCase().replace(/\s+/g, '-');
  const category = ($('skillFormCategory').value||'').trim();
  const content = $('skillFormContent').value;
  const errEl = $('skillFormError');
  errEl.style.display = 'none';
  if (!name) { errEl.textContent = 'Skill name is required'; errEl.style.display = ''; return; }
  if (!content.trim()) { errEl.textContent = 'Content is required'; errEl.style.display = ''; return; }
  try {
    await api('/api/skills/save', {method:'POST', body: JSON.stringify({name, category: category||undefined, content})});
    showToast(_editingSkillName ? 'Skill updated ✓' : 'Skill created ✓');
    _skillsData = null;
    toggleSkillForm();
    await loadSkills();
  } catch(e) { errEl.textContent = 'Error: ' + e.message; errEl.style.display = ''; }
}

// ── Memory inline edit ──
let _memoryData = null;

function toggleMemoryEdit() {
  const form = $('memoryEditForm');
  if (!form) return;
  const open = form.style.display !== 'none';
  if (open) { form.style.display = 'none'; return; }
  $('memEditSection').textContent = 'memory (notes)';
  $('memEditContent').value = _memoryData ? (_memoryData.memory || '') : '';
  $('memEditError').style.display = 'none';
  form.style.display = '';
}

function closeMemoryEdit() {
  const form = $('memoryEditForm');
  if (form) form.style.display = 'none';
}

async function submitMemorySave() {
  const content = $('memEditContent').value;
  const errEl = $('memEditError');
  errEl.style.display = 'none';
  try {
    await api('/api/memory/write', {method:'POST', body: JSON.stringify({section: 'memory', content})});
    showToast('Memory saved ✓');
    closeMemoryEdit();
    await loadMemory(true);
  } catch(e) { errEl.textContent = 'Error: ' + e.message; errEl.style.display = ''; }
}

// ── Workspace management ──
let _workspaceList = [];  // cached from /api/workspaces

function getWorkspaceFriendlyName(path){
  // Look up the friendly name from the workspace list cache, fallback to last path segment
  if(_workspaceList && _workspaceList.length){
    const match=_workspaceList.find(w=>w.path===path);
    if(match && match.name) return match.name;
  }
  return path.split('/').filter(Boolean).pop()||path;
}

async function loadWorkspaceList(){
  try{
    const data = await api('/api/workspaces');
    _workspaceList = data.workspaces || [];
    // Refresh sidebar display if we have a current session
    if(S.session && S.session.workspace) {
      const sidebarName=$('sidebarWsName');
      const sidebarPath=$('sidebarWsPath');
      if(sidebarName) sidebarName.textContent=getWorkspaceFriendlyName(S.session.workspace);
      if(sidebarPath) sidebarPath.textContent=S.session.workspace;
    }
    return data;
  }catch(e){ return {workspaces:[], last:''}; }
}

function renderWorkspaceDropdown(workspaces, currentWs){
  const dd = $('wsDropdown');
  if(!dd)return;
  dd.innerHTML='';
  for(const w of workspaces){
    const opt=document.createElement('div');
    opt.className='ws-opt'+(w.path===currentWs?' active':'');
    opt.innerHTML=`<span class="ws-opt-name">${esc(w.name)}</span><span class="ws-opt-path">${esc(w.path)}</span>`;
    opt.onclick=async()=>{
      closeWsDropdown();
      if(!S.session||w.path===S.session.workspace)return;
      await api('/api/session/update',{method:'POST',body:JSON.stringify({
        session_id:S.session.session_id, workspace:w.path, model:S.session.model
      })});
      S.session.workspace=w.path;
      syncTopbar();
      await loadDir('.');
      showToast(`Switched to ${w.name}`);
    };
    dd.appendChild(opt);
  }
  // Divider + Manage link
  const div=document.createElement('div');div.className='ws-divider';dd.appendChild(div);
  const mgmt=document.createElement('div');mgmt.className='ws-opt ws-manage';
  mgmt.innerHTML='&#9881; Manage workspaces';
  mgmt.onclick=()=>{closeWsDropdown();switchPanel('workspaces');};
  dd.appendChild(mgmt);
}

function toggleWsDropdown(){
  const dd=$('wsDropdown');
  if(!dd)return;
  const open=dd.classList.contains('open');
  if(open){closeWsDropdown();}
  else{
    loadWorkspaceList().then(data=>{
      renderWorkspaceDropdown(data.workspaces, S.session?S.session.workspace:'');
      dd.classList.add('open');
    });
  }
}

function closeWsDropdown(){
  const dd=$('wsDropdown');
  if(dd)dd.classList.remove('open');
}
document.addEventListener('click',e=>{
  if(!e.target.closest('#wsChipWrap'))closeWsDropdown();
});

async function loadWorkspacesPanel(){
  const panel=$('workspacesPanel');
  if(!panel)return;
  const data=await loadWorkspaceList();
  renderWorkspacesPanel(data.workspaces);
}

function renderWorkspacesPanel(workspaces){
  const panel=$('workspacesPanel');
  panel.innerHTML='';
  for(const w of workspaces){
    const row=document.createElement('div');row.className='ws-row';
    row.innerHTML=`
      <div class="ws-row-info">
        <div class="ws-row-name">${esc(w.name)}</div>
        <div class="ws-row-path">${esc(w.path)}</div>
      </div>
      <div class="ws-row-actions">
        <button class="ws-action-btn" title="Use in current session" onclick="switchToWorkspace('${esc(w.path)}','${esc(w.name)}')">&#8594; Use</button>
        <button class="ws-action-btn danger" title="Remove" onclick="removeWorkspace('${esc(w.path)}')">&#10005;</button>
      </div>`;
    panel.appendChild(row);
  }
  const addRow=document.createElement('div');addRow.className='ws-add-row';
  addRow.innerHTML=`
    <input id="wsAddInput" placeholder="Add workspace path (e.g. /home/hermes/CodePath)" style="flex:1;background:rgba(255,255,255,.06);border:1px solid var(--border2);border-radius:7px;color:var(--text);padding:7px 10px;font-size:12px;outline:none;">
    <button class="ws-action-btn" onclick="addWorkspace()">&#43; Add</button>`;
  panel.appendChild(addRow);
  const hint=document.createElement('div');
  hint.style.cssText='font-size:11px;color:var(--muted);padding:4px 0 8px';
  hint.textContent='Paths are validated as existing directories before saving.';
  panel.appendChild(hint);
}

async function addWorkspace(){
  const input=$('wsAddInput');
  const path=(input?input.value:'').trim();
  if(!path)return;
  try{
    const data=await api('/api/workspaces/add',{method:'POST',body:JSON.stringify({path})});
    _workspaceList=data.workspaces;
    renderWorkspacesPanel(data.workspaces);
    if(input)input.value='';
    showToast('Workspace added');
  }catch(e){setStatus('Add failed: '+e.message);}
}

async function removeWorkspace(path){
  if(!confirm(`Remove workspace "${path}"?`))return;
  try{
    const data=await api('/api/workspaces/remove',{method:'POST',body:JSON.stringify({path})});
    _workspaceList=data.workspaces;
    renderWorkspacesPanel(data.workspaces);
    showToast('Workspace removed');
  }catch(e){setStatus('Remove failed: '+e.message);}
}

async function switchToWorkspace(path,name){
  if(!S.session)return;
  try{
    await api('/api/session/update',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id, workspace:path, model:S.session.model
    })});
    S.session.workspace=path;
    syncTopbar();
    await loadDir('.');
    showToast(`Switched to ${name}`);
  }catch(e){setStatus('Switch failed: '+e.message);}
}

// ── Memory panel ──
async function loadMemory(force) {
  const panel = $('memoryPanel');
  try {
    const data = await api('/api/memory');
    _memoryData = data;  // cache for edit form
    const fmtTime = ts => ts ? new Date(ts*1000).toLocaleString() : '';
    panel.innerHTML = `
      <div class="memory-section">
        <div class="memory-section-title">
          &#129504; My Notes
          <span class="memory-mtime">${fmtTime(data.memory_mtime)}</span>
        </div>
        ${data.memory
          ? `<div class="memory-content preview-md">${renderMd(data.memory)}</div>`
          : '<div class="memory-empty">No notes yet.</div>'}
      </div>
      <div class="memory-section">
        <div class="memory-section-title">
          &#128100; User Profile
          <span class="memory-mtime">${fmtTime(data.user_mtime)}</span>
        </div>
        ${data.user
          ? `<div class="memory-content preview-md">${renderMd(data.user)}</div>`
          : '<div class="memory-empty">No profile yet.</div>'}
      </div>`;
  } catch(e) { panel.innerHTML = `<div style="color:var(--accent);font-size:12px">Error: ${esc(e.message)}</div>`; }
}

// Drag and drop
const wrap=$('composerWrap');let dragCounter=0;
document.addEventListener('dragover',e=>e.preventDefault());
document.addEventListener('dragenter',e=>{e.preventDefault();if(e.dataTransfer.types.includes('Files')){dragCounter++;wrap.classList.add('drag-over');}});
document.addEventListener('dragleave',e=>{dragCounter--;if(dragCounter<=0){dragCounter=0;wrap.classList.remove('drag-over');}});
document.addEventListener('drop',e=>{e.preventDefault();dragCounter=0;wrap.classList.remove('drag-over');const files=Array.from(e.dataTransfer.files);if(files.length){addFiles(files);$('msg').focus();}});

// Event wiring
$('btnSend').onclick=send;
$('btnAttach').onclick=()=>$('fileInput').click();
$('fileInput').onchange=e=>{addFiles(Array.from(e.target.files));e.target.value='';};
$('btnNewChat').onclick=async()=>{await newSession();await renderSessionList();$('msg').focus();};
$('btnDownload').onclick=()=>{
  if(!S.session)return;
  const blob=new Blob([transcript()],{type:'text/markdown'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download=`hermes-${S.session.session_id}.md`;a.click();URL.revokeObjectURL(a.href);
};
$('btnExportJSON').onclick=()=>{
  if(!S.session)return;
  const url=`/api/session/export?session_id=${encodeURIComponent(S.session.session_id)}`;
  const a=document.createElement('a');a.href=url;
  a.download=`hermes-${S.session.session_id}.json`;a.click();
};
// btnRefreshFiles is now panel-icon-btn in header (see HTML)
$('btnClearPreview').onclick=()=>{
  $('previewArea').classList.remove('visible');
  $('previewImg').src='';
  $('previewMd').innerHTML='';
  $('previewCode').textContent='';
  $('previewPathText').textContent='';
  $('fileTree').style.display='';
};
// workspacePath click handler removed -- use topbar workspace chip dropdown instead
$('modelSelect').onchange=async()=>{
  if(!S.session)return;
  await api('/api/session/update',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,workspace:S.session.workspace,model:$('modelSelect').value})});
  S.session.model=$('modelSelect').value;syncTopbar();
};
$('msg').addEventListener('input',autoResize);
$('msg').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
// B14: Cmd/Ctrl+K creates a new chat from anywhere
document.addEventListener('keydown',async e=>{
  if((e.metaKey||e.ctrlKey)&&e.key==='k'){
    e.preventDefault();
    if(!S.busy){await newSession();await renderSessionList();$('msg').focus();}
  }
});
$('msg').addEventListener('paste',e=>{
  const items=Array.from(e.clipboardData?.items||[]);
  const imageItems=items.filter(i=>i.type.startsWith('image/'));
  if(!imageItems.length)return;
  e.preventDefault();
  const files=imageItems.map(i=>{
    const blob=i.getAsFile();
    const ext=i.type.split('/')[1]||'png';
    return new File([blob],`screenshot-${Date.now()}.${ext}`,{type:i.type});
  });
  addFiles(files);
  setStatus(`Image pasted: ${files.map(f=>f.name).join(', ')}`);
});
document.querySelectorAll('.suggestion').forEach(btn=>{
  btn.onclick=()=>{$('msg').value=btn.dataset.msg;send();};
});

// Boot: restore last session or start fresh
// ── Resizable panels ──────────────────────────────────────────────────────
(function(){
  const SIDEBAR_MIN=180, SIDEBAR_MAX=420;
  const PANEL_MIN=180,   PANEL_MAX=500;

  function initResize(handleId, targetEl, edge, minW, maxW, storageKey){
    const handle = $(handleId);
    if(!handle || !targetEl) return;

    // Restore saved width
    const saved = localStorage.getItem(storageKey);
    if(saved) targetEl.style.width = saved + 'px';

    let startX=0, startW=0;

    handle.addEventListener('mousedown', e=>{
      e.preventDefault();
      startX = e.clientX;
      startW = targetEl.getBoundingClientRect().width;
      handle.classList.add('dragging');
      document.body.classList.add('resizing');

      const onMove = ev=>{
        const delta = edge==='right' ? ev.clientX - startX : startX - ev.clientX;
        const newW = Math.min(maxW, Math.max(minW, startW + delta));
        targetEl.style.width = newW + 'px';
      };
      const onUp = ()=>{
        handle.classList.remove('dragging');
        document.body.classList.remove('resizing');
        localStorage.setItem(storageKey, parseInt(targetEl.style.width));
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  // Run after DOM ready (called from boot)
  window._initResizePanels = function(){
    const sidebar    = document.querySelector('.sidebar');
    const rightpanel = document.querySelector('.rightpanel');
    initResize('sidebarResize',    sidebar,    'right', SIDEBAR_MIN, SIDEBAR_MAX, 'hermes-sidebar-w');
    initResize('rightpanelResize', rightpanel, 'left',  PANEL_MIN,   PANEL_MAX,   'hermes-panel-w');
  };
})();

(async()=>{
  // Pre-load workspace list so sidebar name is correct from first render
  await loadWorkspaceList();
  _initResizePanels();
  const saved=localStorage.getItem('hermes-webui-session');
  if(saved){
    try{await loadSession(saved);await renderSessionList();await checkInflightOnBoot(saved);return;}
    catch(e){localStorage.removeItem('hermes-webui-session');}
  }
  // no saved session - show empty state, wait for user to hit +
  $('emptyState').style.display='';
  await renderSessionList();
})();
