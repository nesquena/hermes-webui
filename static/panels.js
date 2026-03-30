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
  if (name === 'todos') loadTodos();
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

function loadTodos() {
  const panel = $('todoPanel');
  if (!panel) return;
  // Parse the most recent todo state from message history
  let todos = [];
  for (let i = S.messages.length - 1; i >= 0; i--) {
    const m = S.messages[i];
    if (m && m.role === 'tool') {
      try {
        const d = JSON.parse(typeof m.content === 'string' ? m.content : JSON.stringify(m.content));
        if (d && Array.isArray(d.todos) && d.todos.length) {
          todos = d.todos;
          break;
        }
      } catch(e) {}
    }
  }
  if (!todos.length) {
    panel.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:4px 0">No active task list in this session.</div>';
    return;
  }
  const statusIcon = {pending:'○', in_progress:'◉', completed:'✓', cancelled:'✗'};
  const statusColor = {pending:'var(--muted)', in_progress:'var(--blue)', completed:'rgba(100,200,100,.8)', cancelled:'rgba(200,100,100,.5)'};
  panel.innerHTML = todos.map(t => `
    <div style="display:flex;align-items:flex-start;gap:10px;padding:6px 0;border-bottom:1px solid var(--border);">
      <span style="font-size:14px;flex-shrink:0;margin-top:1px;color:${statusColor[t.status]||'var(--muted)'}">${statusIcon[t.status]||'○'}</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;color:${t.status==='completed'?'var(--muted)':t.status==='in_progress'?'var(--text)':'var(--text)'};${t.status==='completed'?'text-decoration:line-through;opacity:.5':''};line-height:1.4">${esc(t.content)}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:2px;opacity:.6">${esc(t.id)} · ${esc(t.status)}</div>
      </div>
    </div>`).join('');
}

async function clearConversation() {
  if(!S.session) return;
  if(!confirm('Clear all messages in this conversation? This cannot be undone.')) return;
  try {
    const data = await api('/api/session/clear', {method:'POST',
      body: JSON.stringify({session_id: S.session.session_id})});
    S.session = data.session;
    S.messages = [];
    S.toolCalls = [];
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
