/* Neo Meetings panel — room creation, Jitsi embed, post-meeting flow. */

let _meetingsLoaded = false;
let _meetingsData = [];
let _activeMeeting = null;

async function loadMeetingsPanel() {
  const container = document.getElementById('meetingsContent');
  if (!container) return;
  try {
    const resp = await fetch('/api/meetings');
    const data = await resp.json();
    _meetingsData = data.meetings || [];
  } catch (e) {
    _meetingsData = [];
  }
  _meetingsLoaded = true;
  renderMeetingsPanel();
}

function renderMeetingsPanel() {
  const container = document.getElementById('meetingsContent');
  if (!container) return;

  if (_activeMeeting && _activeMeeting.status === 'active') {
    renderActiveMeeting(container);
    return;
  }

  if (_activeMeeting && _activeMeeting.status === 'finished') {
    renderPostMeeting(container);
    return;
  }

  let html = '';
  html += renderMeetingForm();

  if (_meetingsData.length === 0) {
    html += `<p class="meetings-empty" data-i18n="meetings_empty">${t('meetings_empty')}</p>`;
  } else {
    html += '<div class="meetings-list">';
    for (const m of _meetingsData) {
      html += renderMeetingCard(m);
    }
    html += '</div>';
  }

  container.innerHTML = html;
}

function renderMeetingForm() {
  return `
    <div class="meetings-form" id="meetingsForm">
      <div class="meetings-form-row">
        <label for="meetingTitle">${t('title') || 'Title'}</label>
        <input type="text" id="meetingTitle" class="input" placeholder="Sprint Review, Briefing..." />
      </div>
      <div class="meetings-form-row">
        <label for="meetingProject" data-i18n="meetings_project">${t('meetings_project')}</label>
        <input type="text" id="meetingProject" class="input" placeholder="obreiro, brabus, 300..." />
      </div>
      <div class="meetings-form-row">
        <label for="meetingObjective" data-i18n="meetings_objective">${t('meetings_objective')}</label>
        <select id="meetingObjective" class="input">
          <option value="alinhamento">${t('meetings_obj_alinhamento')}</option>
          <option value="homologacao">${t('meetings_obj_homologacao')}</option>
          <option value="fechamento_sprint">${t('meetings_obj_fechamento_sprint')}</option>
          <option value="briefing">${t('meetings_obj_briefing')}</option>
          <option value="suporte">${t('meetings_obj_suporte')}</option>
          <option value="outro">${t('meetings_obj_outro')}</option>
        </select>
      </div>
      <div class="meetings-form-row">
        <label>${t('meetings_participants')}</label>
        <div class="meetings-participants-list" id="meetingsParticipantsList"></div>
        <button type="button" class="meetings-add-participant-btn" onclick="addParticipantRow()">+ ${t('meetings_add_participant')}</button>
      </div>
      <button class="neo-btn--primary" onclick="createMeetingFromForm()" data-i18n="meetings_generate_room">${t('meetings_generate_room')}</button>
    </div>
  `;
}

function addParticipantRow(data) {
  const list = document.getElementById('meetingsParticipantsList');
  if (!list) return;
  const idx = list.children.length;
  const p = data || { name: '', email: '', whatsapp: '', role: 'guest' };
  const row = document.createElement('div');
  row.className = 'meetings-participant-row';
  row.innerHTML = `
    <div class="meetings-participant-fields">
      <input type="text" class="input mp-name" placeholder="${t('meetings_participant_name')}" value="${_mesc(p.name)}" required />
      <input type="email" class="input mp-email" placeholder="${t('meetings_participant_email')}" value="${_mesc(p.email)}" />
      <input type="text" class="input mp-whatsapp" placeholder="${t('meetings_participant_whatsapp')}" value="${_mesc(p.whatsapp)}" />
      <select class="input mp-role">
        <option value="guest"${p.role === 'guest' ? ' selected' : ''}>${t('meetings_role_guest')}</option>
        <option value="client"${p.role === 'client' ? ' selected' : ''}>${t('meetings_role_client')}</option>
        <option value="team"${p.role === 'team' ? ' selected' : ''}>${t('meetings_role_team')}</option>
        <option value="host"${p.role === 'host' ? ' selected' : ''}>${t('meetings_role_host')}</option>
      </select>
    </div>
    <button type="button" class="meetings-participant-remove" onclick="this.closest('.meetings-participant-row').remove()" title="${t('meetings_participant_remove')}">×</button>
  `;
  list.appendChild(row);
}

function getParticipantsFromForm() {
  const rows = document.querySelectorAll('#meetingsParticipantsList .meetings-participant-row');
  const participants = [];
  rows.forEach(row => {
    const name = row.querySelector('.mp-name')?.value?.trim();
    if (!name) return;
    participants.push({
      name,
      email: row.querySelector('.mp-email')?.value?.trim() || '',
      whatsapp: row.querySelector('.mp-whatsapp')?.value?.trim() || '',
      role: row.querySelector('.mp-role')?.value || 'guest',
    });
  });
  return participants;
}

function renderMeetingCard(meeting) {
  const statusKey = 'meetings_status_' + meeting.status;
  const statusLabel = t(statusKey) || meeting.status;
  const date = new Date(meeting.created_at * 1000).toLocaleDateString();
  return `
    <div class="meetings-card meetings-card--${meeting.status}" data-meeting-id="${meeting.id}">
      <div class="meetings-card-header">
        <strong>${_mesc(meeting.title)}</strong>
        <span class="meetings-card-status badge badge--${meeting.status}">${statusLabel}</span>
      </div>
      <div class="meetings-card-meta">
        <span>${_mesc(meeting.project)}</span> · <span>${date}</span>
      </div>
      ${meeting.status === 'planned' ? `<button class="btn btn-sm" onclick="joinMeeting('${meeting.id}')">▶ ${t('meetings_generate_room')}</button>` : ''}
      ${meeting.status === 'finished' ? `<button class="btn btn-sm" onclick="openPostMeeting('${meeting.id}')">📋 ${t('meetings_post_title')}</button>` : ''}
    </div>
  `;
}

function _mesc(str) {
  const el = document.createElement('span');
  el.textContent = str || '';
  return el.innerHTML;
}

async function createMeetingFromForm() {
  const title = document.getElementById('meetingTitle')?.value?.trim();
  const project = document.getElementById('meetingProject')?.value?.trim();
  const objective = document.getElementById('meetingObjective')?.value || 'alinhamento';
  const participants = getParticipantsFromForm();

  if (!title || !project) {
    if (typeof showToast === 'function') showToast('Title and project required', 2500, 'warning');
    return;
  }

  try {
    const resp = await fetch('/api/meetings/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, project, objective, participants }),
    });
    const data = await resp.json();
    if (data.ok) {
      _activeMeeting = data.meeting;
      await startAndEmbed(data.meeting);
    } else {
      if (typeof showToast === 'function') showToast(data.error || 'Error', 2500, 'error');
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Network error', 2500, 'error');
  }
}

async function joinMeeting(meetingId) {
  try {
    const resp = await fetch(`/api/meetings/${meetingId}/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const data = await resp.json();
    if (data.ok) {
      _activeMeeting = data.meeting;
      renderMeetingsPanel();
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Error starting meeting', 2500, 'error');
  }
}

async function startAndEmbed(meeting) {
  try {
    await fetch(`/api/meetings/${meeting.id}/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    _activeMeeting.status = 'active';
  } catch (e) { /* proceed anyway */ }
  renderMeetingsPanel();
}

function renderActiveMeeting(container) {
  const m = _activeMeeting;
  container.innerHTML = `
    <div class="meetings-active">
      <div class="meetings-active-header">
        <h3>${_mesc(m.title)}</h3>
        <span class="badge badge--active">${t('meetings_status_active')}</span>
      </div>
      <div class="meetings-active-actions">
        <a href="${_mesc(m.room_url)}" target="_blank" rel="noopener" class="btn-sm">${t('meetings_open_tab')}</a>
        <button class="btn-sm btn-danger" onclick="endCurrentMeeting()">⏹ ${t('meetings_end')}</button>
      </div>
      <div class="meetings-iframe-wrapper" id="meetingsIframeWrapper">
        <iframe
          id="meetingsJitsiFrame"
          src="${_mesc(m.room_url)}"
          allow="camera; microphone; display-capture; autoplay; clipboard-write"
          allowfullscreen
          style="width:100%; height:100%; border:none; border-radius:8px;"
        ></iframe>
      </div>
    </div>
  `;
}

async function endCurrentMeeting() {
  if (!_activeMeeting) return;
  try {
    const resp = await fetch(`/api/meetings/${_activeMeeting.id}/finish`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const data = await resp.json();
    if (data.ok) {
      _activeMeeting = data.meeting;
      renderMeetingsPanel();
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Error ending meeting', 2500, 'error');
  }
}

function renderPostMeeting(container) {
  const m = _activeMeeting;
  container.innerHTML = `
    <div class="meetings-post">
      <div class="meetings-post-header">
        <h3>${_mesc(m.title)} — ${t('meetings_post_title')}</h3>
        <span class="badge badge--finished">${t('meetings_status_finished')}</span>
      </div>
      <div class="meetings-post-info">
        <p><strong>${t('meetings_project')}:</strong> ${_mesc(m.project)}</p>
        <p><strong>${t('meetings_objective')}:</strong> ${t('meetings_obj_' + m.objective)}</p>
        ${m.participants.length ? `<p><strong>${t('meetings_participants')}:</strong> ${m.participants.map(p => typeof p === 'string' ? _mesc(p) : _mesc(p.name)).join(', ')}</p>` : ''}
      </div>
      <div class="meetings-post-actions">
        <button class="btn" onclick="generateMeetingSummary()">
          📝 ${t('meetings_post_summary')}
        </button>
        <button class="btn" onclick="saveMeetingToObsidian()" disabled title="Phase 2">
          📓 ${t('meetings_post_obsidian')}
        </button>
        <button class="btn" onclick="createMeetingJiraTask()" disabled title="Phase 2">
          🎫 ${t('meetings_post_jira')}
        </button>
      </div>
      <div id="meetingsSummaryOutput" class="meetings-summary-output"></div>
      <div class="meetings-post-footer">
        <button class="btn" onclick="closeMeetingView()">← ${t('tab_meetings')}</button>
      </div>
    </div>
  `;
}

function openPostMeeting(meetingId) {
  const meeting = _meetingsData.find(m => m.id === meetingId);
  if (meeting) {
    _activeMeeting = meeting;
    renderMeetingsPanel();
  }
}

function generateMeetingSummary() {
  if (!_activeMeeting) return;
  const prompt = `Reunião "${_activeMeeting.title}" (projeto: ${_activeMeeting.project}, objetivo: ${_activeMeeting.objective}) acaba de terminar. ` +
    `Participantes: ${_activeMeeting.participants.join(', ') || 'não informados'}. ` +
    `Gere um resumo estruturado com: 1) Resumo objetivo, 2) Decisões tomadas, 3) Pendências e responsáveis, 4) Tarefas candidatas para Jira, 5) Próximos passos.`;

  if (typeof switchPanel === 'function') switchPanel('chat');
  setTimeout(() => {
    const input = document.getElementById('msg');
    if (input) {
      input.value = prompt;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      if (typeof showToast === 'function') showToast(t('meetings_post_summary'), 2000, 'info');
    }
  }, 300);
}

function saveMeetingToObsidian() {
  if (typeof showToast === 'function') showToast('Phase 2 — not yet implemented', 2500, 'info');
}

function createMeetingJiraTask() {
  if (typeof showToast === 'function') showToast('Phase 2 — not yet implemented', 2500, 'info');
}

function closeMeetingView() {
  _activeMeeting = null;
  loadMeetingsPanel();
}

function showMeetingForm() {
  _activeMeeting = null;
  renderMeetingsPanel();
  setTimeout(() => {
    document.getElementById('meetingTitle')?.focus();
  }, 100);
}
