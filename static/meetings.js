/* Neo Meetings panel — room creation, Jitsi embed, post-meeting flow. */

let _meetingsLoaded = false;
let _meetingsData = [];
let _activeMeeting = null;
let _stagedParticipants = [];

let _meetingsState = {
  search: '',
  status: 'all',
  project: 'all',
  period: 'all',
  sort: 'date_desc',
  page: 1,
  pageSize: 10,
};

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

  if (_activeMeeting && _activeMeeting.status === 'processed') {
    renderPostMeeting(container);
    return;
  }

  container.innerHTML = `
    <div class="meetings-panel-layout">
      <div id="meetingsFormContainer"></div>
      <div id="meetingsToolbarContainer"></div>
      <div id="meetingsTableContainer"></div>
      <div id="meetingsPaginationContainer"></div>
    </div>
  `;

  renderMeetingForm();
  renderMeetingsToolbar();
  updateMeetingsList();
}

function renderMeetingForm() {
  const container = document.getElementById('meetingsFormContainer');
  if (!container) return;

  _stagedParticipants = [];

  container.innerHTML = `
    <div class="meetings-form" id="meetingsForm">
      <div class="meetings-form-header">
        <h4 data-i18n="meetings_new">${t('meetings_new')}</h4>
        <p data-i18n="meetings_subtitle">${t('meetings_subtitle')}</p>
      </div>
      
      <div class="meetings-form-grid">
        <div class="meetings-form-row">
          <label for="meetingTitle" data-i18n="meetings_form_title">${t('meetings_form_title')}</label>
          <input type="text" id="meetingTitle" class="input" placeholder="${t('meetings_title_placeholder')}" required />
        </div>
        <div class="meetings-form-row">
          <label for="meetingProject" data-i18n="meetings_project">${t('meetings_project')}</label>
          <input type="text" id="meetingProject" class="input" placeholder="${t('meetings_project_placeholder')}" required />
        </div>
        <div class="meetings-form-row">
          <label for="meetingScheduledAt" data-i18n="meetings_scheduled_at">${t('meetings_scheduled_at')}</label>
          <input type="datetime-local" id="meetingScheduledAt" class="input" placeholder="${t('meetings_scheduled_at_placeholder')}" />
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
      </div>

      <div class="meetings-form-expandable">
        <button type="button" class="meetings-expand-toggle" onclick="toggleParticipantsSection()">
          <span class="arrow">▶</span> <span data-i18n="meetings_participants">${t('meetings_participants')}</span>
        </button>
        <div class="meetings-participants-section" id="meetingsParticipantsSection" style="display: none;">
          <div class="staged-participants-container" id="stagedParticipantsContainer" style="display: none;">
            <div class="staged-participants-list" id="stagedParticipantsList"></div>
          </div>
          
          <div class="meetings-participant-input-form">
            <div class="meetings-participant-fields">
              <input type="text" id="newParticipantName" class="input mp-name" placeholder="${t('meetings_participant_name')}" />
              <input type="email" id="newParticipantEmail" class="input mp-email" placeholder="${t('meetings_participant_email')}" />
              <input type="text" id="newParticipantWhatsapp" class="input mp-whatsapp" placeholder="${t('meetings_participant_whatsapp')}" />
              <select id="newParticipantRole" class="input mp-role">
                <option value="guest" selected>${t('meetings_role_guest')}</option>
                <option value="client">${t('meetings_role_client')}</option>
                <option value="team">${t('meetings_role_team')}</option>
                <option value="host">${t('meetings_role_host')}</option>
              </select>
            </div>
            <button type="button" class="meetings-add-participant-btn" onclick="addStagedParticipant()">+ ${t('meetings_add_participant')}</button>
          </div>
        </div>
      </div>

      <div class="meetings-form-submit">
        <button class="neo-btn neo-btn--primary" onclick="createMeetingFromForm()" id="btnSubmitMeeting">
          ${t('meetings_generate_room')}
        </button>
      </div>
    </div>
  `;
  
  const dtInput = document.getElementById('meetingScheduledAt');
  const btn = document.getElementById('btnSubmitMeeting');
  if (dtInput && btn) {
    dtInput.addEventListener('input', () => {
      if (dtInput.value) {
        btn.textContent = t('meetings_status_planned') || 'Agendar';
      } else {
        btn.textContent = t('meetings_generate_room') || 'Gerar Sala';
      }
    });
  }
}

function toggleParticipantsSection() {
  const section = document.getElementById('meetingsParticipantsSection');
  const toggle = document.querySelector('.meetings-expand-toggle');
  if (!section || !toggle) return;
  const isHidden = section.style.display === 'none';
  if (isHidden) {
    section.style.display = 'block';
    toggle.querySelector('.arrow').textContent = '▼';
    toggle.classList.add('expanded');
  } else {
    section.style.display = 'none';
    toggle.querySelector('.arrow').textContent = '▶';
    toggle.classList.remove('expanded');
  }
}

function addStagedParticipant() {
  const nameInput = document.getElementById('newParticipantName');
  const emailInput = document.getElementById('newParticipantEmail');
  const whatsappInput = document.getElementById('newParticipantWhatsapp');
  const roleSelect = document.getElementById('newParticipantRole');

  if (!nameInput) return;

  const name = nameInput.value.trim();
  if (!name) {
    const errorMsg = t('meetings_participant_name_required') || 'Nome do participante é obrigatório';
    if (typeof showToast === 'function') {
      showToast(errorMsg, 2000, 'warning');
    }
    nameInput.focus();
    return;
  }

  const email = emailInput ? emailInput.value.trim() : '';
  const whatsapp = whatsappInput ? whatsappInput.value.trim() : '';
  const role = roleSelect ? roleSelect.value : 'guest';

  _stagedParticipants.push({ name, email, whatsapp, role });

  // Clear inputs
  nameInput.value = '';
  if (emailInput) emailInput.value = '';
  if (whatsappInput) whatsappInput.value = '';
  if (roleSelect) roleSelect.value = 'guest';

  renderStagedParticipants();
  nameInput.focus();
}

function renderStagedParticipants() {
  const container = document.getElementById('stagedParticipantsContainer');
  const list = document.getElementById('stagedParticipantsList');
  if (!container || !list) return;

  if (_stagedParticipants.length === 0) {
    container.style.display = 'none';
    list.innerHTML = '';
    return;
  }

  container.style.display = 'block';
  list.innerHTML = _stagedParticipants.map((p, idx) => {
    let icon = '👤';
    let roleLabel = t('meetings_role_guest') || 'Convidado';
    if (p.role === 'host') {
      icon = '👑';
      roleLabel = t('meetings_role_host') || 'Anfitrião';
    } else if (p.role === 'client') {
      icon = '🤝';
      roleLabel = t('meetings_role_client') || 'Cliente';
    } else if (p.role === 'team') {
      icon = '💻';
      roleLabel = t('meetings_role_team') || 'Equipe';
    }

    const contactInfo = [p.email, p.whatsapp].filter(Boolean).join(' • ');
    const contactHtml = contactInfo ? `<span class="staged-participant-contact">${_mesc(contactInfo)}</span>` : '';

    return `
      <div class="staged-participant-card role-${p.role}">
        <div class="staged-participant-info">
          <span class="staged-participant-icon" title="${roleLabel}">${icon}</span>
          <div class="staged-participant-details">
            <div class="staged-participant-name-wrapper">
              <span class="staged-participant-name">${_mesc(p.name)}</span>
              <span class="staged-participant-role-badge role-${p.role}">${roleLabel}</span>
            </div>
            ${contactHtml}
          </div>
        </div>
        <div class="staged-participant-actions">
          <button type="button" class="staged-participant-btn edit-btn" onclick="editStagedParticipant(${idx})" title="Editar">${li('pencil', 14)}</button>
          <button type="button" class="staged-participant-btn remove-btn" onclick="removeStagedParticipant(${idx})" title="Remover">${li('x', 14)}</button>
        </div>
      </div>
    `;
  }).join('');
}

function editStagedParticipant(idx) {
  const p = _stagedParticipants[idx];
  if (!p) return;

  const nameInput = document.getElementById('newParticipantName');
  const emailInput = document.getElementById('newParticipantEmail');
  const whatsappInput = document.getElementById('newParticipantWhatsapp');
  const roleSelect = document.getElementById('newParticipantRole');

  if (nameInput) nameInput.value = p.name;
  if (emailInput) emailInput.value = p.email;
  if (whatsappInput) whatsappInput.value = p.whatsapp;
  if (roleSelect) roleSelect.value = p.role;

  _stagedParticipants.splice(idx, 1);
  renderStagedParticipants();

  if (nameInput) nameInput.focus();
}

function removeStagedParticipant(idx) {
  _stagedParticipants.splice(idx, 1);
  renderStagedParticipants();
}

function getParticipantsFromForm() {
  const nameInput = document.getElementById('newParticipantName');
  const emailInput = document.getElementById('newParticipantEmail');
  const whatsappInput = document.getElementById('newParticipantWhatsapp');
  const roleSelect = document.getElementById('newParticipantRole');
  
  const currentParticipants = [..._stagedParticipants];
  
  if (nameInput && nameInput.value.trim()) {
    currentParticipants.push({
      name: nameInput.value.trim(),
      email: emailInput ? emailInput.value.trim() : '',
      whatsapp: whatsappInput ? whatsappInput.value.trim() : '',
      role: roleSelect ? roleSelect.value : 'guest'
    });
  }
  
  return currentParticipants;
}

function renderMeetingsToolbar() {
  const container = document.getElementById('meetingsToolbarContainer');
  if (!container) return;

  const projects = new Set();
  _meetingsData.forEach(m => {
    if (m.project) projects.add(m.project);
  });
  const projectOptions = Array.from(projects).sort();

  container.innerHTML = `
    <div class="meetings-toolbar">
      <div class="meetings-filter-item search-field">
        <label for="filterSearch" data-i18n="topbar_search">${t('topbar_search')}</label>
        <input type="text" id="filterSearch" class="input" placeholder="${t('meetings_search_placeholder')}" value="${_mesc(_meetingsState.search)}" />
      </div>

      <div class="meetings-filter-item">
        <label for="filterStatus" data-i18n="meetings_filter_status">${t('meetings_filter_status')}</label>
        <select id="filterStatus" class="input">
          <option value="all">Todos</option>
          <option value="planned">${t('meetings_status_planned')}</option>
          <option value="active">${t('meetings_status_active')}</option>
          <option value="finished">${t('meetings_status_finished')}</option>
          <option value="processed">${t('meetings_status_processed')}</option>
        </select>
      </div>

      <div class="meetings-filter-item">
        <label for="filterProject" data-i18n="meetings_filter_project">${t('meetings_filter_project')}</label>
        <select id="filterProject" class="input">
          <option value="all">Todos</option>
          ${projectOptions.map(p => `<option value="${_mesc(p)}">${_mesc(p)}</option>`).join('')}
        </select>
      </div>

      <div class="meetings-filter-item">
        <label for="filterPeriod" data-i18n="meetings_filter_period">${t('meetings_filter_period')}</label>
        <select id="filterPeriod" class="input">
          <option value="all">Todos</option>
          <option value="today">Hoje</option>
          <option value="tomorrow">Amanhã</option>
          <option value="next_7">Próximos 7 dias</option>
          <option value="prev_7">Últimos 7 dias</option>
          <option value="month">Mês atual</option>
        </select>
      </div>

      <div class="meetings-filter-item">
        <label for="filterSort" data-i18n="meetings_sort_by">${t('meetings_sort_by')}</label>
        <select id="filterSort" class="input">
          <option value="date_desc">Data/hora (Recente)</option>
          <option value="date_asc">Data/hora (Antiga)</option>
          <option value="status">Status</option>
          <option value="project">Projeto/Cliente</option>
          <option value="title">Título</option>
        </select>
      </div>
    </div>
  `;

  document.getElementById('filterStatus').value = _meetingsState.status;
  document.getElementById('filterProject').value = _meetingsState.project;
  document.getElementById('filterPeriod').value = _meetingsState.period;
  document.getElementById('filterSort').value = _meetingsState.sort;

  const searchInput = document.getElementById('filterSearch');
  searchInput.addEventListener('input', () => {
    _meetingsState.search = searchInput.value;
    _meetingsState.page = 1;
    updateMeetingsList();
  });

  ['filterStatus', 'filterProject', 'filterPeriod', 'filterSort'].forEach(id => {
    document.getElementById(id).addEventListener('change', (e) => {
      const key = id.replace('filter', '').toLowerCase();
      _meetingsState[key] = e.target.value;
      _meetingsState.page = 1;
      updateMeetingsList();
    });
  });
}

function getFilteredMeetings() {
  let list = [..._meetingsData];

  if (_meetingsState.search.trim()) {
    const q = _meetingsState.search.toLowerCase().trim();
    list = list.filter(m => 
      (m.title && m.title.toLowerCase().includes(q)) || 
      (m.project && m.project.toLowerCase().includes(q))
    );
  }

  if (_meetingsState.status !== 'all') {
    list = list.filter(m => m.status === _meetingsState.status);
  }

  if (_meetingsState.project !== 'all') {
    list = list.filter(m => m.project === _meetingsState.project);
  }

  if (_meetingsState.period !== 'all') {
    const now = new Date();
    const getStartOfDay = (d) => {
      const res = new Date(d);
      res.setHours(0, 0, 0, 0);
      return res;
    };
    const startOfToday = getStartOfDay(now);
    const startOfTomorrow = new Date(startOfToday.getTime() + 86400000);
    const endOfTomorrow = new Date(startOfTomorrow.getTime() + 86400000);
    const endOfToday = startOfTomorrow;

    list = list.filter(m => {
      const mDateVal = m.scheduled_at || m.started_at || m.created_at;
      if (!mDateVal) return false;
      const mDate = new Date(mDateVal * 1000);

      switch (_meetingsState.period) {
        case 'today':
          return mDate >= startOfToday && mDate < endOfToday;
        case 'tomorrow':
          return mDate >= startOfTomorrow && mDate < endOfTomorrow;
        case 'next_7':
          const next7 = new Date(startOfToday.getTime() + 7 * 86400000);
          return mDate >= startOfToday && mDate < next7;
        case 'prev_7':
          const prev7 = new Date(startOfToday.getTime() - 7 * 86400000);
          return mDate >= prev7 && mDate < endOfToday;
        case 'month':
          return mDate.getMonth() === now.getMonth() && mDate.getFullYear() === now.getFullYear();
        default:
          return true;
      }
    });
  }

  list.sort((a, b) => {
    const getPrimaryDate = (m) => m.scheduled_at || m.started_at || m.created_at || 0;
    
    switch (_meetingsState.sort) {
      case 'date_desc':
        return getPrimaryDate(b) - getPrimaryDate(a);
      case 'date_asc':
        return getPrimaryDate(a) - getPrimaryDate(b);
      case 'status':
        return (a.status || '').localeCompare(b.status || '');
      case 'project':
        return (a.project || '').localeCompare(b.project || '');
      case 'title':
        return (a.title || '').localeCompare(b.title || '');
      default:
        return 0;
    }
  });

  return list;
}

function formatMeetingPrimaryDate(meeting) {
  let dateVal = meeting.scheduled_at;
  let typeLabel = '';
  
  if (dateVal) {
    typeLabel = `<span class="date-type scheduled" title="Agendada">📅</span> `;
  } else {
    dateVal = meeting.started_at || meeting.created_at;
    typeLabel = `<span class="date-type created" title="Criada/Iniciada">⏱</span> `;
  }

  if (!dateVal) return '-';

  try {
    const d = new Date(dateVal * 1000);
    const day = String(d.getDate()).padStart(2, '0');
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const year = d.getFullYear();
    const hours = String(d.getHours()).padStart(2, '0');
    const minutes = String(d.getMinutes()).padStart(2, '0');
    return `${typeLabel}${day}/${month}/${year} ${hours}:${minutes}`;
  } catch (e) {
    return '-';
  }
}

function updateMeetingsList() {
  const tableContainer = document.getElementById('meetingsTableContainer');
  const paginationContainer = document.getElementById('meetingsPaginationContainer');
  if (!tableContainer || !paginationContainer) return;

  const filtered = getFilteredMeetings();

  if (filtered.length === 0) {
    tableContainer.innerHTML = `
      <div class="meetings-empty-container">
        <p class="meetings-empty" data-i18n="meetings_no_results">${t('meetings_no_results')}</p>
      </div>
    `;
    paginationContainer.innerHTML = '';
    return;
  }

  const total = filtered.length;
  const maxPages = Math.ceil(total / _meetingsState.pageSize);
  if (_meetingsState.page > maxPages) {
    _meetingsState.page = Math.max(1, maxPages);
  }

  const startIndex = (_meetingsState.page - 1) * _meetingsState.pageSize;
  const paginatedItems = filtered.slice(startIndex, startIndex + _meetingsState.pageSize);

  tableContainer.innerHTML = renderMeetingsTable(paginatedItems);
  paginationContainer.innerHTML = renderMeetingsPagination(total, paginatedItems.length);
}

function renderMeetingsTable(items) {
  let html = `
    <div class="meetings-table-wrap">
      <table class="meetings-table">
        <thead>
          <tr>
            <th data-i18n="meetings_form_title">${t('meetings_form_title')}</th>
            <th data-i18n="meetings_project">${t('meetings_project')}</th>
            <th data-i18n="meetings_objective">${t('meetings_objective')}</th>
            <th data-i18n="meetings_scheduled_at">${t('meetings_scheduled_at')}</th>
            <th data-i18n="meetings_participants">${t('meetings_participants')}</th>
            <th data-i18n="meetings_filter_status">${t('meetings_filter_status')}</th>
            <th data-i18n="meetings_actions">${t('meetings_actions')}</th>
          </tr>
        </thead>
        <tbody>
  `;

  for (const m of items) {
    const statusKey = 'meetings_status_' + m.status;
    const statusLabel = t(statusKey) || m.status;
    const dateFormatted = formatMeetingPrimaryDate(m);
    const participantsCount = m.participants ? m.participants.length : 0;
    
    const objKey = 'meetings_obj_' + m.objective;
    const objLabel = t(objKey) || m.objective;

    let participantNames = '';
    if (m.participants && m.participants.length > 0) {
      participantNames = m.participants.map(p => typeof p === 'string' ? p : p.name).join(', ');
    }

    html += `
      <tr class="meetings-row meetings-row--${m.status}" data-meeting-id="${m.id}">
        <td class="meetings-cell-title" title="${_mesc(m.title)}">
          <strong>${_mesc(m.title)}</strong>
        </td>
        <td class="meetings-cell-project">
          <span class="meetings-project-badge">${_mesc(m.project)}</span>
        </td>
        <td class="meetings-cell-objective">${_mesc(objLabel)}</td>
        <td class="meetings-cell-date">${dateFormatted}</td>
        <td class="meetings-cell-participants" title="${_mesc(participantNames)}">
          ${participantsCount}
        </td>
        <td>
          <span class="badge badge--${m.status}">${statusLabel}</span>
        </td>
        <td class="meetings-cell-actions">
          <div class="meetings-row-actions">
            ${m.status === 'planned' ? `
              <button class="btn btn-sm btn-icon" onclick="joinMeeting('${m.id}')" title="${t('meetings_open_room') || 'Abrir sala'}">▶</button>
            ` : ''}
            ${m.status === 'active' ? `
              <button class="btn btn-sm btn-icon btn-active-room" onclick="openActiveRoomFromTable('${m.id}')" title="${t('meetings_open_room') || 'Abrir sala'}">▶</button>
              <button class="btn btn-sm btn-icon btn-danger" onclick="endMeetingFromTable('${m.id}')" title="${t('meetings_end') || 'Encerrar'}">⏹</button>
            ` : ''}
            ${m.status === 'finished' ? `
              <button class="btn btn-sm btn-icon" onclick="openPostMeeting('${m.id}')" title="${t('meetings_post_summary') || 'Gerar resumo'}">📝</button>
            ` : ''}
            ${m.status === 'processed' ? `
              <button class="btn btn-sm btn-icon" onclick="openPostMeeting('${m.id}')" title="${t('meetings_post_title') || 'Detalhes'}">📋</button>
            ` : ''}
            <button class="btn btn-sm btn-icon" onclick="copyMeetingLink('${m.id}')" title="${t('meetings_copy_link') || 'Copiar link'}">🔗</button>
            <button class="btn btn-sm btn-icon btn-details" onclick="openPostMeeting('${m.id}')" title="${t('meetings_post_title') || 'Detalhes'}">ℹ</button>
          </div>
        </td>
      </tr>
    `;
  }

  html += `
        </tbody>
      </table>
    </div>
  `;
  return html;
}

function renderMeetingsPagination(total, pageItemsLength) {
  const maxPages = Math.ceil(total / _meetingsState.pageSize);
  
  let html = `
    <div class="meetings-pagination">
      <div class="meetings-pagination-info">
        <span class="total-count">${total} ${t('meetings_table_title').toLowerCase()}</span>
      </div>
      
      <div class="meetings-pagination-controls">
        <button class="btn btn-sm" onclick="changeMeetingsPage(-1)" ${_meetingsState.page <= 1 ? 'disabled' : ''}>
          ${t('meetings_prev') || 'Anterior'}
        </button>
        <span class="page-indicator">${t('meetings_prev') ? 'Página' : 'Page'} ${_meetingsState.page} / ${maxPages}</span>
        <button class="btn btn-sm" onclick="changeMeetingsPage(1)" ${_meetingsState.page >= maxPages ? 'disabled' : ''}>
          ${t('meetings_next') || 'Próxima'}
        </button>
      </div>

      <div class="meetings-pagination-size">
        <label for="filterPageSize">${t('meetings_page_size') || 'Itens por página'}:</label>
        <select id="filterPageSize" class="input input-sm">
          <option value="5">5</option>
          <option value="10">10</option>
          <option value="25">25</option>
        </select>
      </div>
    </div>
  `;
  
  setTimeout(() => {
    const sizeSelect = document.getElementById('filterPageSize');
    if (sizeSelect) {
      sizeSelect.value = _meetingsState.pageSize;
      sizeSelect.addEventListener('change', (e) => {
        _meetingsState.pageSize = parseInt(e.target.value, 10);
        _meetingsState.page = 1;
        updateMeetingsList();
      });
    }
  }, 0);

  return html;
}

function changeMeetingsPage(delta) {
  _meetingsState.page += delta;
  updateMeetingsList();
}

function openActiveRoomFromTable(meetingId) {
  const meeting = _meetingsData.find(m => m.id === meetingId);
  if (meeting) {
    _activeMeeting = meeting;
    renderMeetingsPanel();
  }
}

async function endMeetingFromTable(meetingId) {
  try {
    const resp = await fetch(`/api/meetings/${meetingId}/finish`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const data = await resp.json();
    if (data.ok) {
      loadMeetingsPanel();
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Error ending meeting', 2500, 'error');
  }
}

function copyMeetingLink(meetingId) {
  const meeting = _meetingsData.find(m => m.id === meetingId);
  if (!meeting || !meeting.room_url) return;
  navigator.clipboard.writeText(meeting.room_url).then(() => {
    if (typeof showToast === 'function') {
      const copyMsg = t('meetings_copy_link') || 'Link copiado';
      showToast(`${copyMsg}!`, 2000, 'success');
    }
  }).catch(() => {
    if (typeof showToast === 'function') showToast('Error copying link', 2000, 'error');
  });
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
  
  const dtInput = document.getElementById('meetingScheduledAt')?.value;
  let scheduled_at = null;
  if (dtInput) {
    scheduled_at = new Date(dtInput).getTime() / 1000;
  }

  if (!title || !project) {
    const errorMsg = t('meetings_required_error') || 'Title and project required';
    if (typeof showToast === 'function') showToast(errorMsg, 2500, 'warning');
    return;
  }

  try {
    const resp = await fetch('/api/meetings/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, project, objective, participants, scheduled_at }),
    });
    const data = await resp.json();
    if (data.ok) {
      if (scheduled_at && scheduled_at > Date.now() / 1000) {
        if (typeof showToast === 'function') showToast(t('meetings_status_planned') || 'Reunião agendada', 2500, 'success');
        loadMeetingsPanel();
      } else {
        _activeMeeting = data.meeting;
        await startAndEmbed(data.meeting);
      }
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

function formatMeetingDuration(startedAt, finishedAt) {
  if (!startedAt || !finishedAt) return null;
  const diff = finishedAt - startedAt;
  if (diff < 0) return null;
  const mins = Math.floor(diff / 60);
  const secs = Math.floor(diff % 60);
  if (mins > 0) {
    return `${mins} min ${secs}s`;
  }
  return `${secs}s`;
}

function renderPostMeeting(container) {
  const m = _activeMeeting;
  if (!m) return;

  const formatDate = (ts) => {
    if (!ts) return '-';
    try {
      return new Date(ts * 1000).toLocaleString();
    } catch (e) {
      return '-';
    }
  };

  const statusKey = 'meetings_status_' + m.status;
  const statusLabel = t(statusKey) || m.status;
  const objKey = 'meetings_obj_' + m.objective;
  const objLabel = t(objKey) || m.objective;
  const duration = formatMeetingDuration(m.started_at, m.finished_at);

  const getRoleLabel = (role) => {
    const mapping = {
      host: t('meetings_role_host') || 'Host',
      client: t('meetings_role_client') || 'Cliente',
      team: t('meetings_role_team') || 'Equipe',
      guest: t('meetings_role_guest') || 'Convidado'
    };
    return mapping[role] || role;
  };

  container.innerHTML = `
    <div class="meetings-post-container">
      <div class="meetings-post-card-header">
        <div class="meetings-post-title-group">
          <span class="meetings-post-icon">📅</span>
          <h3>${_mesc(m.title)}</h3>
          <span class="badge badge--${m.status}">${statusLabel}</span>
        </div>
      </div>

      <div class="meetings-post-grid">
        <div class="meetings-post-info-card">
          <h4 class="card-subtitle">📁 ${t('tab_workspace') || 'Geral'}</h4>
          <div class="info-row">
            <span class="info-label">${t('meetings_project')}:</span>
            <span class="info-value"><span class="meetings-project-badge">${_mesc(m.project)}</span></span>
          </div>
          <div class="info-row">
            <span class="info-label">${t('meetings_objective')}:</span>
            <span class="info-value">${_mesc(objLabel)}</span>
          </div>
          <div class="info-row vertical">
            <span class="info-label">${t('meetings_participants')}:</span>
            <div class="participants-pill-list">
              ${m.participants && m.participants.length ? 
                m.participants.map(p => {
                  const name = typeof p === 'string' ? p : p.name;
                  const role = typeof p === 'string' ? 'guest' : (p.role || 'guest');
                  const roleLabel = getRoleLabel(role);
                  let icon = '👤';
                  if (role === 'host') icon = '👑';
                  else if (role === 'client') icon = '🤝';
                  else if (role === 'team') icon = '💻';
                  return `<span class="participant-pill role-${role}" title="${roleLabel}"><span style="margin-right: 4px;">${icon}</span>${_mesc(name)}</span>`;
                }).join('') : `<span class="no-participants">${t('meetings_empty') || 'Nenhum'}</span>`
              }
            </div>
          </div>
        </div>

        <div class="meetings-post-info-card">
          <h4 class="card-subtitle">⏱ Linha do Tempo</h4>
          ${m.scheduled_at ? `
          <div class="info-row">
            <span class="info-label">${t('meetings_scheduled_at') || 'Agendada para'}:</span>
            <span class="info-value">${formatDate(m.scheduled_at)}</span>
          </div>` : ''}
          <div class="info-row">
            <span class="info-label">Criada em:</span>
            <span class="info-value">${formatDate(m.created_at)}</span>
          </div>
          ${m.started_at ? `
          <div class="info-row">
            <span class="info-label">Iniciada em:</span>
            <span class="info-value">${formatDate(m.started_at)}</span>
          </div>` : ''}
          ${m.finished_at ? `
          <div class="info-row">
            <span class="info-label">Finalizada em:</span>
            <span class="info-value">${formatDate(m.finished_at)}</span>
          </div>` : ''}
          ${duration ? `
          <div class="info-row duration-row">
            <span class="info-label">Duração:</span>
            <span class="info-value highlight">${duration}</span>
          </div>` : ''}
        </div>
      </div>

      <div class="meetings-post-actions-bar">
        ${(m.status === 'finished' || m.status === 'processed') ? `
          <button class="neo-btn neo-btn--primary" onclick="generateMeetingSummary()">
            📝 ${t('meetings_post_summary')}
          </button>
        ` : ''}
        ${m.status === 'planned' ? `
          <button class="neo-btn neo-btn--primary" onclick="joinMeeting('${m.id}')">
            ▶ ${t('meetings_open_room') || 'Abrir Sala'}
          </button>
        ` : ''}
        ${m.status === 'active' ? `
          <a href="${_mesc(m.room_url)}" target="_blank" rel="noopener" class="neo-btn neo-btn--primary">${t('meetings_open_tab')}</a>
          <button class="neo-btn neo-btn--danger" onclick="endCurrentMeeting()">
            ⏹ ${t('meetings_end')}
          </button>
        ` : ''}
        <button class="neo-btn neo-btn--secondary" onclick="copyMeetingLink('${m.id}')">
          🔗 ${t('meetings_copy_link') || 'Copiar Link'}
        </button>
      </div>

      ${m.summary ? `
        <div class="meetings-post-summary-section">
          <div class="summary-section-header">
            <span class="icon">📝</span>
            <h4>Resumo da Reunião</h4>
          </div>
          <div class="summary-section-body">
            <pre class="summary-pre">${_mesc(typeof m.summary === 'string' ? m.summary : JSON.stringify(m.summary, null, 2))}</pre>
          </div>
        </div>
      ` : ''}

      <div class="meetings-post-footer-nav">
        <button class="neo-btn neo-btn--text" onclick="closeMeetingView()">← ${t('meetings_table_title') || 'Voltar para Tabela'}</button>
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
    `Participantes: ${_activeMeeting.participants.map(p => typeof p === 'string' ? p : p.name).join(', ') || 'não informados'}. ` +
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
