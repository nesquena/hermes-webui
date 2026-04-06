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
  if (name === 'profiles') await loadProfilesPanel();
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
            <div class="cron-last-header" style="display:flex;align-items:center;justify-content:space-between">
              <span>Last output</span>
              <button class="cron-btn" style="padding:1px 8px;font-size:10px" onclick="loadCronHistory('${job.id}',this)">All runs</button>
            </div>
            <div class="cron-last" id="cron-out-text-${job.id}" style="color:var(--muted);font-size:11px">Loading…</div>
            <div id="cron-history-${job.id}" style="display:none"></div>
          </div>
        </div>`;
      box.appendChild(item);
      // Eagerly load last output for visible items
      loadCronOutput(job.id);
    }
  } catch(e) { box.innerHTML = `<div style="padding:12px;color:var(--accent);font-size:12px">Error: ${esc(e.message)}</div>`; }
}

let _cronSelectedSkills=[];
let _cronSkillsCache=null;

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
    _cronSelectedSkills=[];
    _renderCronSkillTags();
    const search=$('cronFormSkillSearch');
    if(search)search.value='';
    // Pre-fetch skills for the picker
    if(!_cronSkillsCache){
      api('/api/skills').then(d=>{_cronSkillsCache=d.skills||[];}).catch(()=>{});
    }
    $('cronFormName').focus();
  }
}

function _renderCronSkillTags(){
  const wrap=$('cronFormSkillTags');
  if(!wrap)return;
  wrap.innerHTML='';
  for(const name of _cronSelectedSkills){
    const tag=document.createElement('span');
    tag.className='skill-tag';
    tag.dataset.skill=name;
    const rm=document.createElement('span');
    rm.className='remove-tag';rm.textContent='×';
    rm.onclick=()=>{_cronSelectedSkills=_cronSelectedSkills.filter(s=>s!==name);tag.remove();};
    tag.appendChild(document.createTextNode(name));
    tag.appendChild(rm);
    wrap.appendChild(tag);
  }
}

// Skill search input handler
(function(){
  const setup=()=>{
    const search=$('cronFormSkillSearch');
    const dropdown=$('cronFormSkillDropdown');
    if(!search||!dropdown)return;
    search.oninput=()=>{
      const q=search.value.trim().toLowerCase();
      if(!q||!_cronSkillsCache){dropdown.style.display='none';return;}
      const matches=_cronSkillsCache.filter(s=>
        !_cronSelectedSkills.includes(s.name)&&
        (s.name.toLowerCase().includes(q)||(s.category||'').toLowerCase().includes(q))
      ).slice(0,8);
      if(!matches.length){dropdown.style.display='none';return;}
      dropdown.innerHTML='';
      for(const s of matches){
        const opt=document.createElement('div');
        opt.className='skill-opt';
        opt.textContent=s.name+(s.category?' ('+s.category+')':'');
        opt.onclick=()=>{
          _cronSelectedSkills.push(s.name);
          _renderCronSkillTags();
          search.value='';
          dropdown.style.display='none';
        };
        dropdown.appendChild(opt);
      }
      dropdown.style.display='';
    };
    search.onblur=()=>setTimeout(()=>{dropdown.style.display='none';},150);
  };
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',setup);
  else setTimeout(setup,0);
})();

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
    const body={schedule,prompt,deliver};
    if(name)body.name=name;
    if(_cronSelectedSkills.length)body.skills=_cronSelectedSkills;
    await api('/api/crons/create',{method:'POST',body:JSON.stringify(body)});
    toggleCronForm();
    showToast('Job created ✓');
    await loadCrons();
  }catch(e){
    errEl.textContent='Error: '+e.message;errEl.style.display='';
  }
}

function _cronOutputSnippet(content) {
  // Extract the response body from a cron output .md file
  const lines = content.split('\n');
  const responseIdx = lines.findIndex(l => l.startsWith('## Response') || l.startsWith('# Response'));
  const body = (responseIdx >= 0 ? lines.slice(responseIdx + 1) : lines).join('\n').trim();
  return body.slice(0, 600) || '(empty)';
}

async function loadCronOutput(jobId) {
  try {
    const data = await api(`/api/crons/output?job_id=${encodeURIComponent(jobId)}&limit=1`);
    const el = $('cron-out-text-' + jobId);
    if (!el) return;
    if (!data.outputs || !data.outputs.length) { el.textContent = '(no runs yet)'; return; }
    const out = data.outputs[0];
    const ts = out.filename.replace('.md','').replace(/_/g,' ');
    el.textContent = ts + '\n\n' + _cronOutputSnippet(out.content);
  } catch(e) { /* ignore */ }
}

async function loadCronHistory(jobId, btn) {
  const histEl = $('cron-history-' + jobId);
  if (!histEl) return;
  // Toggle: if already open, close it
  if (histEl.style.display !== 'none') {
    histEl.style.display = 'none';
    if (btn) btn.textContent = 'All runs';
    return;
  }
  if (btn) btn.textContent = 'Loading…';
  try {
    const data = await api(`/api/crons/output?job_id=${encodeURIComponent(jobId)}&limit=20`);
    if (!data.outputs || !data.outputs.length) {
      histEl.innerHTML = '<div style="font-size:11px;color:var(--muted);padding:4px 0">(no runs yet)</div>';
    } else {
      histEl.innerHTML = data.outputs.map((out, i) => {
        const ts = out.filename.replace('.md','').replace(/_/g,' ');
        const snippet = _cronOutputSnippet(out.content);
        const id = `cron-hist-run-${jobId}-${i}`;
        return `<div style="border-top:1px solid var(--border);padding:6px 0">
          <div style="display:flex;align-items:center;justify-content:space-between;cursor:pointer" onclick="document.getElementById('${id}').style.display=document.getElementById('${id}').style.display==='none'?'':'none'">
            <span style="font-size:11px;font-weight:600;color:var(--muted)">${esc(ts)}</span>
            <span style="font-size:10px;color:var(--muted);opacity:.6">▸</span>
          </div>
          <div id="${id}" style="display:none;font-size:11px;color:var(--muted);white-space:pre-wrap;line-height:1.5;margin-top:4px;max-height:200px;overflow-y:auto">${esc(snippet)}</div>
        </div>`;
      }).join('');
    }
    histEl.style.display = '';
    if (btn) btn.textContent = 'Hide runs';
  } catch(e) {
    if (btn) btn.textContent = 'All runs';
  }
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
    let html = renderMd(data.content || '(no content)');
    // Render linked files section if present
    const lf = data.linked_files || {};
    const categories = Object.entries(lf).filter(([,files]) => files && files.length > 0);
    if (categories.length) {
      html += '<div class="skill-linked-files"><div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Linked Files</div>';
      for (const [cat, files] of categories) {
        html += `<div class="skill-linked-section"><h4>${esc(cat)}</h4>`;
        for (const f of files) {
          html += `<a class="skill-linked-file" href="#" data-skill-name="${esc(name)}" data-skill-file="${esc(f)}">${esc(f)}</a>`;
        }
        html += '</div>';
      }
      html += '</div>';
    }
    $('previewMd').innerHTML = html;
    // Wire linked-file clicks via data attributes (avoids inline JS XSS with apostrophes)
    $('previewMd').querySelectorAll('.skill-linked-file').forEach(a=>{
      a.addEventListener('click',e=>{e.preventDefault();openSkillFile(a.dataset.skillName,a.dataset.skillFile);});
    });
    $('previewArea').classList.add('visible');
    $('fileTree').style.display = 'none';
  } catch(e) { setStatus('Could not load skill: ' + e.message); }
}

async function openSkillFile(skillName, filePath) {
  try {
    const data = await api(`/api/skills/content?name=${encodeURIComponent(skillName)}&file=${encodeURIComponent(filePath)}`);
    $('previewPathText').textContent = skillName + ' / ' + filePath;
    $('previewBadge').textContent = filePath.split('.').pop() || 'file';
    $('previewBadge').className = 'preview-badge code';
    const ext = filePath.split('.').pop() || '';
    if (['md','markdown'].includes(ext)) {
      showPreview('md');
      $('previewMd').innerHTML = renderMd(data.content || '');
    } else {
      showPreview('code');
      $('previewCode').textContent = data.content || '';
      requestAnimationFrame(() => highlightCode());
    }
  } catch(e) { setStatus('Could not load file: ' + e.message); }
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
    closeProfileDropdown(); // close profile dropdown if open
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
  if(!e.target.closest('#sidebarWsDisplay') && !e.target.closest('#wsDropdown'))closeWsDropdown();
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
    <input id="wsAddInput" placeholder="Add workspace path (e.g. /home/user/my-project)" style="flex:1;background:rgba(255,255,255,.06);border:1px solid var(--border2);border-radius:7px;color:var(--text);padding:7px 10px;font-size:12px;outline:none;">
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

// ── Profile panel + dropdown ──
let _profilesCache = null;

async function loadProfilesPanel() {
  const panel = $('profilesPanel');
  if (!panel) return;
  try {
    const data = await api('/api/profiles');
    _profilesCache = data;
    panel.innerHTML = '';
    if (!data.profiles || !data.profiles.length) {
      panel.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:12px">No profiles found.</div>';
      return;
    }
    for (const p of data.profiles) {
      const card = document.createElement('div');
      card.className = 'profile-card';
      const meta = [];
      if (p.model) meta.push(p.model.split('/').pop());
      if (p.provider) meta.push(p.provider);
      if (p.skill_count) meta.push(p.skill_count + ' skill' + (p.skill_count !== 1 ? 's' : ''));
      if (p.has_env) meta.push('API keys configured');
      const gwDot = p.gateway_running
        ? '<span class="profile-opt-badge running" title="Gateway running"></span>'
        : '<span class="profile-opt-badge stopped" title="Gateway stopped"></span>';
      const isActive = p.name === data.active;
      const activeBadge = isActive ? '<span style="color:var(--link);font-size:10px;font-weight:600;margin-left:6px">ACTIVE</span>' : '';
      card.innerHTML = `
        <div class="profile-card-header">
          <div style="min-width:0;flex:1">
            <div class="profile-card-name${isActive ? ' is-active' : ''}">${gwDot}${esc(p.name)}${p.is_default ? ' <span style="opacity:.5">(default)</span>' : ''}${activeBadge}</div>
            ${meta.length ? `<div class="profile-card-meta">${esc(meta.join(' \u00b7 '))}</div>` : '<div class="profile-card-meta">No configuration</div>'}
          </div>
          <div class="profile-card-actions">
            ${!isActive ? `<button class="ws-action-btn" onclick="switchToProfile('${esc(p.name)}')" title="Switch to this profile">Use</button>` : ''}
            ${!p.is_default ? `<button class="ws-action-btn danger" onclick="deleteProfile('${esc(p.name)}')" title="Delete this profile">&#10005;</button>` : ''}
          </div>
        </div>`;
      panel.appendChild(card);
    }
  } catch (e) {
    panel.innerHTML = `<div style="color:var(--accent);font-size:12px;padding:12px">Error: ${esc(e.message)}</div>`;
  }
}

function renderProfileDropdown(data) {
  const dd = $('profileDropdown');
  if (!dd) return;
  dd.innerHTML = '';
  const profiles = data.profiles || [];
  const active = data.active || 'default';
  for (const p of profiles) {
    const opt = document.createElement('div');
    opt.className = 'profile-opt' + (p.name === active ? ' active' : '');
    const meta = [];
    if (p.model) meta.push(p.model.split('/').pop());
    if (p.skill_count) meta.push(p.skill_count + ' skills');
    const gwDot = `<span class="profile-opt-badge ${p.gateway_running ? 'running' : 'stopped'}"></span>`;
    const checkmark = p.name === active ? ' <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--link)" stroke-width="3" style="vertical-align:-1px"><polyline points="20 6 9 17 4 12"/></svg>' : '';
    opt.innerHTML = `<div class="profile-opt-name">${gwDot}${esc(p.name)}${p.is_default ? ' <span style="opacity:.5;font-weight:400">(default)</span>' : ''}${checkmark}</div>` +
      (meta.length ? `<div class="profile-opt-meta">${esc(meta.join(' \u00b7 '))}</div>` : '');
    opt.onclick = async () => {
      closeProfileDropdown();
      if (p.name === active) return;
      await switchToProfile(p.name);
    };
    dd.appendChild(opt);
  }
  // Divider + Manage link
  const div = document.createElement('div'); div.className = 'ws-divider'; dd.appendChild(div);
  const mgmt = document.createElement('div'); mgmt.className = 'profile-opt ws-manage';
  mgmt.innerHTML = '&#9881; Manage profiles';
  mgmt.onclick = () => { closeProfileDropdown(); switchPanel('profiles'); };
  dd.appendChild(mgmt);
}

function toggleProfileDropdown() {
  const dd = $('profileDropdown');
  if (!dd) return;
  if (dd.classList.contains('open')) { closeProfileDropdown(); return; }
  closeWsDropdown(); // close workspace dropdown if open
  api('/api/profiles').then(data => {
    renderProfileDropdown(data);
    dd.classList.add('open');
  }).catch(e => { showToast('Failed to load profiles'); });
}

function closeProfileDropdown() {
  const dd = $('profileDropdown');
  if (dd) dd.classList.remove('open');
}
document.addEventListener('click', e => {
  if (!e.target.closest('#profileChipWrap')) closeProfileDropdown();
});

async function switchToProfile(name) {
  if (S.busy) { showToast('Cannot switch profiles while agent is running'); return; }

  // Determine whether the current session has any messages.
  // A session with messages is "in progress" and belongs to the current profile —
  // we must not retag it.  We'll start a fresh session for the new profile instead.
  const sessionInProgress = S.session && S.messages && S.messages.length > 0;

  try {
    const data = await api('/api/profile/switch', { method: 'POST', body: JSON.stringify({ name }) });
    S.activeProfile = data.active || name;

    // ── Model ──────────────────────────────────────────────────────────────
    localStorage.removeItem('hermes-webui-model');
    _skillsData = null;
    await populateModelDropdown();
    if (data.default_model) {
      const sel = $('modelSelect');
      const resolved = _applyModelToDropdown(data.default_model, sel);
      const modelToUse = resolved || data.default_model;
      S._pendingProfileModel = modelToUse;
      // Only patch the in-memory session model if we're NOT about to replace the session
      if (S.session && !sessionInProgress) {
        S.session.model = modelToUse;
      }
    }

    // ── Workspace ──────────────────────────────────────────────────────────
    _workspaceList = null;
    await loadWorkspaceList();
    if (data.default_workspace) {
      // Always store the profile default for new sessions
      S._profileDefaultWorkspace = data.default_workspace;

      if (S.session && !sessionInProgress) {
        // Empty session (no messages yet) — safe to update it in place
        try {
          await api('/api/session/update', { method: 'POST', body: JSON.stringify({
            session_id: S.session.session_id,
            workspace: data.default_workspace,
            model: S.session.model,
          })});
          S.session.workspace = data.default_workspace;
        } catch (_) {}
      }
    }

    // ── Session ────────────────────────────────────────────────────────────
    _showAllProfiles = false;

    if (sessionInProgress) {
      // The current session has messages and belongs to the previous profile.
      // Start a new session for the new profile so nothing gets cross-tagged.
      await newSession(false);
      await renderSessionList();
      showToast('Switched to profile: ' + name + ' — new conversation started');
    } else {
      // No messages yet — just refresh the list and topbar in place
      await renderSessionList();
      syncTopbar();
      showToast('Switched to profile: ' + name);
    }

    // ── Sidebar panels ─────────────────────────────────────────────────────
    if (_currentPanel === 'skills') await loadSkills();
    if (_currentPanel === 'memory') await loadMemory();
    if (_currentPanel === 'tasks') await loadCrons();
    if (_currentPanel === 'profiles') await loadProfilesPanel();
    if (_currentPanel === 'workspaces') await loadWorkspacesPanel();

  } catch (e) { showToast('Switch failed: ' + e.message); }
}

function toggleProfileForm() {
  const form = $('profileCreateForm');
  if (!form) return;
  form.style.display = form.style.display === 'none' ? '' : 'none';
  if (form.style.display !== 'none') {
    $('profileFormName').value = '';
    $('profileFormClone').checked = false;
    const errEl = $('profileFormError');
    if (errEl) errEl.style.display = 'none';
    $('profileFormName').focus();
  }
}

async function submitProfileCreate() {
  const name = ($('profileFormName').value || '').trim().toLowerCase();
  const cloneConfig = $('profileFormClone').checked;
  const errEl = $('profileFormError');
  if (!name) { errEl.textContent = 'Name is required'; errEl.style.display = ''; return; }
  if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(name)) { errEl.textContent = 'Lowercase letters, numbers, hyphens, underscores only'; errEl.style.display = ''; return; }
  try {
    await api('/api/profile/create', { method: 'POST', body: JSON.stringify({ name, clone_config: cloneConfig }) });
    toggleProfileForm();
    await loadProfilesPanel();
    showToast('Profile created: ' + name);
  } catch (e) { errEl.textContent = e.message || 'Create failed'; errEl.style.display = ''; }
}

async function deleteProfile(name) {
  if (!confirm(`Delete profile "${name}"? This removes all config, skills, memory, and sessions for this profile.`)) return;
  try {
    await api('/api/profile/delete', { method: 'POST', body: JSON.stringify({ name }) });
    await loadProfilesPanel();
    showToast('Profile deleted: ' + name);
  } catch (e) { showToast('Delete failed: ' + e.message); }
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

// ── Settings panel ───────────────────────────────────────────────────────────

function toggleSettings(){
  const overlay=$('settingsOverlay');
  if(!overlay) return;
  if(overlay.style.display==='none'){
    overlay.style.display='';
    loadSettingsPanel();
  } else {
    overlay.style.display='none';
  }
}

async function loadSettingsPanel(){
  try{
    const settings=await api('/api/settings');
    // Populate model dropdown from /api/models
    const modelSel=$('settingsModel');
    if(modelSel){
      modelSel.innerHTML='';
      try{
        const models=await api('/api/models');
        for(const g of (models.groups||[])){
          const og=document.createElement('optgroup');
          og.label=g.provider;
          for(const m of g.models){
            const opt=document.createElement('option');
            opt.value=m.id;opt.textContent=m.label;
            og.appendChild(opt);
          }
          modelSel.appendChild(og);
        }
      }catch(e){}
      modelSel.value=settings.default_model||'';
    }
    // Populate workspace dropdown from /api/workspaces
    const wsSel=$('settingsWorkspace');
    if(wsSel){
      wsSel.innerHTML='';
      try{
        const wsData=await api('/api/workspaces');
        for(const w of (wsData.workspaces||[])){
          const opt=document.createElement('option');
          opt.value=w.path;opt.textContent=w.name||w.path;
          wsSel.appendChild(opt);
        }
      }catch(e){}
      wsSel.value=settings.default_workspace||'';
    }
    // Send key preference
    const sendKeySel=$('settingsSendKey');
    if(sendKeySel) sendKeySel.value=settings.send_key||'enter';
    const showUsageCb=$('settingsShowTokenUsage');
    if(showUsageCb) showUsageCb.checked=!!settings.show_token_usage;
    const showCliCb=$('settingsShowCliSessions');
    if(showCliCb) showCliCb.checked=!!settings.show_cli_sessions;
    const syncCb=$('settingsSyncInsights');
    if(syncCb) syncCb.checked=!!settings.sync_to_insights;
    // Password field: always blank (we don't send hash back)
    const pwField=$('settingsPassword');
    if(pwField) pwField.value='';
    // Show auth buttons only when auth is active
    try{
      const authStatus=await api('/api/auth/status');
      const active=authStatus.auth_enabled;
      const signOutBtn=$('btnSignOut');
      if(signOutBtn) signOutBtn.style.display=active?'':'none';
      const disableBtn=$('btnDisableAuth');
      if(disableBtn) disableBtn.style.display=active?'':'none';
    }catch(e){}
  }catch(e){
    showToast('Failed to load settings: '+e.message);
  }
}

async function saveSettings(){
  const model=($('settingsModel')||{}).value;
  const workspace=($('settingsWorkspace')||{}).value;
  const sendKey=($('settingsSendKey')||{}).value;
  const showTokenUsage=!!($('settingsShowTokenUsage')||{}).checked;
  const showCliSessions=!!($('settingsShowCliSessions')||{}).checked;
  const pw=($('settingsPassword')||{}).value;
  const body={};
  if(model) body.default_model=model;
  const botName=(($('settingsBotName')||{}).value||'').trim()||'Hermes';
  body.bot_name=botName;
  const botName=(($('settingsBotName')||{}).value||'').trim()||'Hermes';
  body.bot_name=botName;
  if(workspace) body.default_workspace=workspace;
  if(sendKey) body.send_key=sendKey;
  body.show_token_usage=showTokenUsage;
  body.show_cli_sessions=showCliSessions;
  body.sync_to_insights=!!($('settingsSyncInsights')||{}).checked;
  // Password: only act if the field has content; blank = leave auth unchanged
  if(pw && pw.trim()){
    try{
      await api('/api/settings',{method:'POST',body:JSON.stringify({...body,_set_password:pw.trim()})});
      window._sendKey=sendKey||'enter';
      window._showTokenUsage=showTokenUsage;
      showToast('Settings saved (password set — login now required)');
      window._botName=botName;applyBotName();
      window._botName=botName;applyBotName();
      toggleSettings();
      return;
    }catch(e){showToast('Save failed: '+e.message);return;}
  }
  try{
    await api('/api/settings',{method:'POST',body:JSON.stringify(body)});
    window._sendKey=sendKey||'enter';
    window._showTokenUsage=showTokenUsage;
    window._showCliSessions=showCliSessions;
    window._botName=botName;applyBotName();
    window._botName=botName;applyBotName();
    renderMessages();
    if(typeof renderSessionList==='function') renderSessionList();
    showToast('Settings saved');
    toggleSettings();
  }catch(e){
    showToast('Save failed: '+e.message);
  }
}

async function signOut(){
  try{
    await api('/api/auth/logout',{method:'POST',body:'{}'});
    window.location.href='/login';
  }catch(e){
    showToast('Sign out failed: '+e.message);
  }
}

async function disableAuth(){
  if(!confirm('Disable password protection? Anyone will be able to access this instance.')) return;
  try{
    await api('/api/settings',{method:'POST',body:JSON.stringify({_clear_password:true})});
    showToast('Auth disabled — password protection removed');
    // Hide both auth buttons since auth is now off
    const disableBtn=$('btnDisableAuth');
    if(disableBtn) disableBtn.style.display='none';
    const signOutBtn=$('btnSignOut');
    if(signOutBtn) signOutBtn.style.display='none';
  }catch(e){
    showToast('Failed to disable auth: '+e.message);
  }
}

// Close settings on overlay click (not panel click)
document.addEventListener('click',e=>{
  const overlay=$('settingsOverlay');
  if(overlay&&e.target===overlay) toggleSettings();
});

// ── Cron completion alerts ────────────────────────────────────────────────────

let _cronPollSince=Date.now()/1000;  // track from page load
let _cronPollTimer=null;
let _cronUnreadCount=0;

function startCronPolling(){
  if(_cronPollTimer) return;
  _cronPollTimer=setInterval(async()=>{
    if(document.hidden) return;  // don't poll when tab is in background
    try{
      const data=await api(`/api/crons/recent?since=${_cronPollSince}`);
      if(data.completions&&data.completions.length>0){
        for(const c of data.completions){
          const icon=c.status==='error'?'\u274c':'\u2705';
          showToast(`${icon} Cron "${c.name}" ${c.status==='error'?'failed':'completed'}`,4000);
          _cronPollSince=Math.max(_cronPollSince,c.completed_at);
        }
        _cronUnreadCount+=data.completions.length;
        updateCronBadge();
      }
    }catch(e){}
  },30000);
}

function updateCronBadge(){
  const tab=document.querySelector('.nav-tab[data-panel="tasks"]');
  if(!tab) return;
  let badge=tab.querySelector('.cron-badge');
  if(_cronUnreadCount>0){
    if(!badge){
      badge=document.createElement('span');
      badge.className='cron-badge';
      tab.style.position='relative';
      tab.appendChild(badge);
    }
    badge.textContent=_cronUnreadCount>9?'9+':_cronUnreadCount;
    badge.style.display='';
  }else if(badge){
    badge.style.display='none';
  }
}

// Clear cron badge when Tasks tab is opened
const _origSwitchPanel=switchPanel;
switchPanel=async function(name){
  if(name==='tasks'){_cronUnreadCount=0;updateCronBadge();}
  return _origSwitchPanel(name);
};

// Start polling on page load
startCronPolling();

// ── Background agent error tracking ──────────────────────────────────────────

const _backgroundErrors=[];  // {session_id, title, message, ts}

function trackBackgroundError(sessionId, title, message){
  // Only track if user is NOT currently viewing this session
  if(S.session&&S.session.session_id===sessionId) return;
  _backgroundErrors.push({session_id:sessionId, title:title||'Untitled', message, ts:Date.now()});
  showErrorBanner();
}

function showErrorBanner(){
  let banner=$('bgErrorBanner');
  if(!banner){
    banner=document.createElement('div');
    banner.id='bgErrorBanner';
    banner.className='bg-error-banner';
    const msgs=document.querySelector('.messages');
    if(msgs) msgs.parentNode.insertBefore(banner,msgs);
    else document.body.appendChild(banner);
  }
  const latest=_backgroundErrors[0];  // FIFO: show oldest (first) error
  if(!latest){banner.style.display='none';return;}
  const count=_backgroundErrors.length;
  banner.innerHTML=`<span>\u26a0 ${count>1?count+' sessions have':'"'+esc(latest.title)+'" has'} encountered an error</span><div style="display:flex;gap:6px;flex-shrink:0"><button class="reconnect-btn" onclick="navigateToErrorSession()">View</button><button class="reconnect-btn" onclick="dismissErrorBanner()">Dismiss</button></div>`;
  banner.style.display='';
}

function navigateToErrorSession(){
  const latest=_backgroundErrors.shift();  // FIFO: show oldest error first
  if(latest){
    loadSession(latest.session_id);renderSessionList();
  }
  if(_backgroundErrors.length===0) dismissErrorBanner();
  else showErrorBanner();
}

function dismissErrorBanner(){
  _backgroundErrors.length=0;
  const banner=$('bgErrorBanner');
  if(banner) banner.style.display='none';
}

// Event wiring
