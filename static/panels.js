let _currentPanel = 'chat';
let _renamingAppTitlebar = false;  // guard against re-entrant rename
let _kanbanBoard = null;
let _kanbanLatestEventId = 0;
let _kanbanPollTimer = null;
let _kanbanCurrentTaskId = null;
let _kanbanLanesByProfile = false;
// Multi-board state. _kanbanCurrentBoard is the slug of the active board
// the UI is currently viewing. null means "use whatever the server reports
// as active" (i.e. don't pin a specific board in API calls). The UI
// persists the last-viewed slug to localStorage so refresh stays put.
let _kanbanCurrentBoard = null;
let _kanbanBoardsList = null;
let _kanbanBoardMenuOpen = false;
// SSE event stream — replaces the 30s polling cadence with a long-lived
// /api/kanban/events/stream connection. Falls back to polling when the
// EventSource fails to connect (proxy that strips text/event-stream, etc).
let _kanbanEventSource = null;
let _kanbanEventSourceFailures = 0;
let _skillsData = null; // cached skills list
let _cronList = null; // cached cron jobs (array)
let _currentCronDetail = null; // full cron job object
let _cronMode = 'empty'; // 'empty' | 'read' | 'create' | 'edit'
let _cronPreFormDetail = null; // snapshot of prior selection when entering a form
let _currentWorkspaceDetail = null; // { path, name, is_default }
let _workspaceMode = 'empty'; // 'empty' | 'read' | 'create' | 'edit'
let _workspacePreFormDetail = null;
let _currentProfileDetail = null; // full profile object
let _profileMode = 'empty'; // 'empty' | 'read' | 'create'
let _profilePreFormDetail = null;
let _pendingSettingsTargetPanel = null; // destination selected while settings had unsaved changes
let _logsAutoRefreshTimer = null;
let _lastLogsLines = [];

// Map of panel names → i18n keys for the app titlebar label.
const APP_TITLEBAR_KEYS = {
  chat: 'tab_chat', tasks: 'tab_tasks', skills: 'tab_skills',
  memory: 'tab_memory', workspaces: 'tab_workspaces',
  profiles: 'tab_profiles', todos: 'tab_todos', insights: 'tab_insights', logs: 'tab_logs', settings: 'tab_settings',
};

/**
 * Update the top app titlebar to reflect the current page or selected conversation.
 * On the chat panel, a selected session's title takes precedence over the page name.
 */
function syncAppTitlebar() {
  const titleEl = document.getElementById('appTitlebarTitle');
  const subEl = document.getElementById('appTitlebarSub');
  if (!titleEl) return;
  const panel = (typeof _currentPanel === 'string' && _currentPanel) ? _currentPanel : 'chat';
  let mainText = '';
  let subText = '';
  let sourceLabel = '';
  if (panel === 'chat' && typeof S !== 'undefined' && S && S.session) {
    mainText = S.session.title || (typeof t === 'function' ? t('untitled') : 'Untitled');
    const vis = Array.isArray(S.messages) ? S.messages.filter(m => m && m.role && m.role !== 'tool') : [];
    if (typeof t === 'function') subText = t('n_messages', vis.length);
    if (S.session.is_cli_session) sourceLabel = S.session.source_label || S.session.source_tag || S.session.raw_source || '';
  } else {
    const key = APP_TITLEBAR_KEYS[panel];
    mainText = key && typeof t === 'function' ? t(key) : (panel.charAt(0).toUpperCase() + panel.slice(1));
  }

  // Don't touch the element while an inline rename is in progress — replacing
  // the span with an input would fire a MutationObserver that calls
  // syncAppTitlebar again, destroying the input before the user finishes.
  if (_renamingAppTitlebar) return;

  titleEl.textContent = mainText;
  if (subEl) {
    if (subText) {
      subEl.textContent = subText;
      if (sourceLabel) {
        const badge = document.createElement('span');
        badge.className = 'topbar-source-badge';
        badge.textContent = sourceLabel + (S.session && S.session.read_only ? ' · read-only' : '');
        subEl.appendChild(document.createTextNode(' '));
        subEl.appendChild(badge);
      }
      subEl.hidden = false;
    }
    else { subEl.textContent = ''; subEl.hidden = true; }
  }

  // Double-click on the titlebar title → rename the active session (same behaviour
  // as double-clicking a session title in the sidebar).  Only active on the chat
  // panel when a session is open.
  titleEl.ondblclick = null;  // remove any previous handler before adding a fresh one
  if (panel === 'chat' && typeof S !== 'undefined' && S && S.session && !(S.session.read_only || S.session.is_read_only)) {
    titleEl.ondblclick = (e) => {
      e.stopPropagation();
      e.preventDefault();
      if (_renamingAppTitlebar) return;
      _renamingAppTitlebar = true;

      const inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'app-titlebar-rename-input';
      inp.value = S.session.title || (typeof t === 'function' ? t('untitled') : 'Untitled');

      // Prevent click/dblclick on the input from bubbling — we don't want
      // panel switches, session switches, or any other handler firing.
      ['click', 'mousedown', 'dblclick', 'pointerdown'].forEach(ev =>
        inp.addEventListener(ev, e2 => e2.stopPropagation())
      );

      const finish = async (save) => {
        _renamingAppTitlebar = false;
        if (save) {
          const newTitle = inp.value.trim() || (typeof t === 'function' ? t('untitled') : 'Untitled');
          S.session.title = newTitle;
          syncTopbar();   // update #topbarTitle in the chat header
          syncAppTitlebar();
          // Update the sidebar list so the renamed title appears immediately.
          // _renderOneSession reads from _allSessions cache, so patch it there too.
          try {
            const _cached = typeof _allSessions !== 'undefined' && _allSessions.find(s => s && s.session_id === S.session.session_id);
            if (_cached) _cached.title = newTitle;
          } catch (_) {}
          if (typeof renderSessionListFromCache === 'function') renderSessionListFromCache();
          try {
            await api('/api/session/rename', {
              method: 'POST',
              body: JSON.stringify({ session_id: S.session.session_id, title: newTitle })
            });
          } catch (err) {
            if (typeof setStatus === 'function') setStatus('Rename failed: ' + err.message);
          }
        }
        inp.replaceWith(titleEl);
        syncAppTitlebar();
      };

      inp.onkeydown = e2 => {
        if (e2.key === 'Enter') { e2.preventDefault(); e2.stopPropagation(); finish(true); }
        if (e2.key === 'Escape') { e2.preventDefault(); e2.stopPropagation(); finish(false); }
      };
      inp.onblur = () => finish(false);

      titleEl.replaceWith(inp);
      inp.focus();
      inp.select();
    };
  }
}

function _beginSettingsPanelSession() {
  _settingsDirty = false;
  _settingsThemeOnOpen = localStorage.getItem('hermes-theme') || 'dark';
  _settingsSkinOnOpen = localStorage.getItem('hermes-skin') || 'default';
  _settingsFontSizeOnOpen = localStorage.getItem('hermes-font-size') || 'default';
  _pendingSettingsTargetPanel = null;
  if (_settingsAppearanceAutosaveTimer) {
    clearTimeout(_settingsAppearanceAutosaveTimer);
    _settingsAppearanceAutosaveTimer = null;
  }
  _settingsAppearanceAutosaveRetryPayload = null;
  _resetSettingsPanelState();
}

function _beforePanelSwitch(nextPanel) {
  if (_currentPanel !== 'settings' || nextPanel === 'settings') return true;
  if (_settingsDirty) {
    _pendingSettingsTargetPanel = nextPanel || 'chat';
    _showSettingsUnsavedBar();
    return false;
  }
  _revertSettingsPreview();
  _pendingSettingsTargetPanel = null;
  _resetSettingsPanelState();
  return true;
}

function _consumeSettingsTargetPanel(fallback = 'chat') {
  const target = (_pendingSettingsTargetPanel && _pendingSettingsTargetPanel !== 'settings')
    ? _pendingSettingsTargetPanel
    : fallback;
  _pendingSettingsTargetPanel = null;
  return target;
}

async function switchPanel(name, opts = {}) {
  const nextPanel = name || 'chat';
  const prevPanel = _currentPanel;
  if (!opts.bypassSettingsGuard && !_beforePanelSwitch(nextPanel)) return false;
  if (prevPanel !== 'settings' && nextPanel === 'settings') _beginSettingsPanelSession();
  // Close any long-lived Kanban SSE stream when leaving the kanban panel
  // so we don't keep a stale connection open in the background.
  if (prevPanel === 'kanban' && nextPanel !== 'kanban') {
    if (typeof _kanbanStopPolling === 'function') _kanbanStopPolling();
  }
  _currentPanel = nextPanel;
  // Update nav tabs (rail + mobile sidebar-nav share data-panel)
  document.querySelectorAll('[data-panel]').forEach(t => t.classList.toggle('active', t.dataset.panel === nextPanel));
  // Update panel views
  document.querySelectorAll('.panel-view').forEach(p => p.classList.remove('active'));
  const panelEl = $('panel' + nextPanel.charAt(0).toUpperCase() + nextPanel.slice(1));
  if (panelEl) panelEl.classList.add('active');
  // Update main content view. Each entry in MAIN_VIEW_PANELS gets a matching
  // showing-<name> class on <main>; no class means chat (the default).
  const mainEl = document.querySelector('main.main');
  if (mainEl) {
    ['settings','skills','memory','tasks','kanban','workspaces','profiles','insights','logs'].forEach(p => {
      mainEl.classList.toggle('showing-' + p, nextPanel === p);
    });
  }
  // Lazy-load panel data
  if (nextPanel === 'tasks') await loadCrons();
  if (nextPanel === 'kanban') await loadKanban();
  if (nextPanel === 'skills') await loadSkills();
  if (nextPanel === 'memory') await loadMemory();
  if (nextPanel === 'workspaces') await loadWorkspacesPanel();
  if (nextPanel === 'profiles') await loadProfilesPanel();
  if (nextPanel === 'todos') loadTodos();
  if (nextPanel === 'insights') await loadInsights();
  if (nextPanel === 'logs') await loadLogs();
  _syncLogsAutoRefresh();
  if (typeof _syncSystemHealthMonitorVisibility === 'function') _syncSystemHealthMonitorVisibility();
  if (nextPanel === 'settings') {
    switchSettingsSection(_currentSettingsSection);
    loadSettingsPanel();
  }
  syncAppTitlebar();
  return true;
}

// ── Cron panel ──
function _isRecurringCronJob(job) {
  const kind = job && job.schedule && job.schedule.kind;
  return kind === 'cron' || kind === 'interval';
}

function _hasUnlimitedRepeat(job) {
  return !!(job && job.repeat && job.repeat.times == null);
}

function _isCronNeedsAttention(job) {
  return _isRecurringCronJob(job) &&
    _hasUnlimitedRepeat(job) &&
    job.enabled === false &&
    job.state === 'completed' &&
    !job.next_run_at;
}

function _isCronScheduleError(job) {
  return _isRecurringCronJob(job) &&
    !job.next_run_at &&
    (job.state === 'error' || job.last_status === 'error');
}

function _cronStatusMeta(job) {
  if (_isCronNeedsAttention(job)) return {
    state: 'needs_attention',
    listClass: 'attention',
    detailClass: 'warn',
    label: t('cron_status_needs_attention'),
  };
  if (_isCronScheduleError(job)) return {
    state: 'schedule_error',
    listClass: 'attention',
    detailClass: 'warn',
    label: t('cron_status_needs_attention'),
  };
  if (job.state === 'paused') return {
    state: 'paused',
    listClass: 'paused',
    detailClass: 'warn',
    label: t('cron_status_paused'),
  };
  if (job.enabled === false) return {
    state: 'off',
    listClass: 'disabled',
    detailClass: 'warn',
    label: t('cron_status_off'),
  };
  if (job.last_status === 'error') return {
    state: 'error',
    listClass: 'error',
    detailClass: 'err',
    label: t('cron_status_error'),
  };
  return {
    state: 'active',
    listClass: 'active',
    detailClass: 'ok',
    label: t('cron_status_active'),
  };
}


function _cronProfileName(profile){
  return (profile || '').toString().trim();
}

function _cronProfileLabel(profile){
  const name = _cronProfileName(profile);
  return name || (t('cron_profile_server_default') || 'server default');
}

function _cronProfileTitle(profile){
  const name = _cronProfileName(profile);
  if (name) return (t('cron_profile_label') || 'Profile') + ': ' + name;
  return t('cron_profile_server_default_hint') || 'Uses the WebUI server default profile at run time';
}

async function loadCronProfiles(){
  if (_cronProfilesCache) return _cronProfilesCache;
  try {
    const data = await api('/api/profiles');
    _cronProfilesCache = Array.isArray(data.profiles) ? data.profiles : [];
  } catch(e) {
    _cronProfilesCache = [];
  }
  return _cronProfilesCache;
}

function _cronProfileOptions(selected){
  const current = _cronProfileName(selected);
  const profiles = Array.isArray(_cronProfilesCache) ? _cronProfilesCache : [];
  const seen = new Set(['']);
  const opts = [`<option value=""${current ? '' : ' selected'}>${esc(t('cron_profile_server_default') || 'server default')}</option>`];
  for (const p of profiles) {
    const name = _cronProfileName(p && p.name);
    if (!name || seen.has(name)) continue;
    seen.add(name);
    const label = p && p.is_default ? `${name} (${t('default') || 'default'})` : name;
    opts.push(`<option value="${esc(name)}"${current === name ? ' selected' : ''}>${esc(label)}</option>`);
  }
  if (current && !seen.has(current)) {
    opts.push(`<option value="${esc(current)}" selected>${esc(current)} (${esc(t('not_available') || 'not available')})</option>`);
  }
  return opts.join('');
}

function _refreshCronProfileSelect(selected){
  const sel = $('cronFormProfile');
  if (!sel) return;
  const keep = selected === undefined ? sel.value : selected;
  sel.innerHTML = _cronProfileOptions(keep);
}

function _cronDiagnostics(job) {
  const fields = {
    id: job.id,
    name: job.name || null,
    schedule: job.schedule || null,
    schedule_display: job.schedule_display || null,
    enabled: job.enabled,
    state: job.state,
    next_run_at: job.next_run_at || null,
    last_run_at: job.last_run_at || null,
    last_status: job.last_status || null,
    last_error: job.last_error || null,
    last_delivery_error: job.last_delivery_error || null,
    repeat: job.repeat || null,
    deliver: job.deliver || null,
  };
  return JSON.stringify(fields, null, 2);
}

async function loadCrons(animate) {
  const box = $('cronList');
  const refreshBtn = $('cronRefreshBtn');
  if (animate && refreshBtn) {
    refreshBtn.style.opacity = '0.5';
    refreshBtn.disabled = true;
  }
  try {
    await loadCronProfiles();
    const data = await api('/api/crons');
    _cronList = data.jobs || [];
    if (!_cronList.length) {
      box.innerHTML = `<div style="padding:16px;color:var(--muted);font-size:12px">${esc(t('cron_no_jobs'))}</div>`;
      if (_cronMode !== 'create' && _cronMode !== 'edit') _clearCronDetail();
      return;
    }
    box.innerHTML = '';
    for (const job of _cronList) {
      const item = document.createElement('div');
      item.className = 'cron-item';
      item.id = 'cron-' + job.id;
      const status = _cronStatusMeta(job);
      const isNewRun = _cronNewJobIds.has(String(job.id));
      const profileLabel = _cronProfileLabel(job.profile);
      const profileTitle = _cronProfileTitle(job.profile);
      item.innerHTML = `
        <div class="cron-header">
          ${isNewRun ? '<span class="cron-new-dot" title="New run"></span>' : ''}
          <span class="cron-name" title="${esc(job.name)}">${esc(job.name)}</span>
          <span class="cron-profile-badge" title="${esc(profileTitle)}">${esc(profileLabel)}</span>
          <span class="cron-status ${status.listClass}">${esc(status.label)}</span>
        </div>`;
      item.onclick = () => openCronDetail(job.id, item);
      if (_currentCronDetail && _currentCronDetail.id === job.id) item.classList.add('active');
      box.appendChild(item);
    }
    // Re-render current detail with fresh data if we have one and we're not in a form
    if (_currentCronDetail && _cronMode !== 'create' && _cronMode !== 'edit') {
      const refreshed = _cronList.find(j => j.id === _currentCronDetail.id);
      if (refreshed) _renderCronDetail(refreshed);
      else _clearCronDetail();
    }
  } catch(e) { box.innerHTML = `<div style="padding:12px;color:var(--accent);font-size:12px">${esc(t('error_prefix'))}${esc(e.message)}</div>`; }
  finally {
    if (animate && refreshBtn) {
      refreshBtn.style.opacity = '';
      refreshBtn.disabled = false;
    }
  }
}

function _renderCronDetail(job){
  _currentCronDetail = job;
  const title = $('taskDetailTitle');
  const body = $('taskDetailBody');
  const empty = $('taskDetailEmpty');
  if (!title || !body) return;
  title.textContent = job.name || job.schedule_display || '(unnamed)';
  const status = _cronStatusMeta(job);
  const nextRun = job.next_run_at ? new Date(job.next_run_at).toLocaleString() : t('not_available');
  const lastRun = job.last_run_at ? new Date(job.last_run_at).toLocaleString() : t('never');
  const schedule = job.schedule_display || (job.schedule && job.schedule.expression) || '';
  const skills = Array.isArray(job.skills) && job.skills.length ? job.skills.join(', ') : '—';
  const deliver = job.deliver || 'local';
  const profileLabel = _cronProfileLabel(job.profile);
  const profileTitle = _cronProfileTitle(job.profile);
  const lastError = job.last_error ? `<div class="detail-row"><div class="detail-row-label">${esc(t('error_prefix').replace(/:\s*$/,''))}</div><div class="detail-row-value" style="color:var(--accent-text)">${esc(job.last_error)}</div></div>` : '';
  const attention = status.state === 'needs_attention' || status.state === 'schedule_error';
  const croniterHint = job.last_error && /croniter/i.test(job.last_error)
    ? `<p>${esc(t('cron_attention_croniter_hint'))}</p>`
    : '';
  const attentionBanner = attention ? `
      <div class="detail-alert cron-attention-panel">
        <div class="detail-alert-title">${esc(t('cron_status_needs_attention'))}</div>
        <p>${esc(t('cron_attention_desc'))}</p>
        ${croniterHint}
        <div class="detail-alert-actions">
          <button type="button" class="cron-btn run" onclick="resumeCurrentCron()">${esc(t('cron_attention_resume'))}</button>
          <button type="button" class="cron-btn" onclick="runCurrentCron()">${esc(t('cron_attention_run_once'))}</button>
          <button type="button" class="cron-btn" onclick="copyCurrentCronDiagnostics()">${esc(t('cron_attention_copy_diagnostics'))}</button>
        </div>
      </div>` : '';
  body.innerHTML = `
    <div class="main-view-content">
      ${attentionBanner}
      <div class="detail-card">
        <div class="detail-card-title">${esc(t('cron_status_active').replace(/./,c=>c.toUpperCase()))}</div>
        <div class="detail-row"><div class="detail-row-label">Status</div><div class="detail-row-value"><span class="detail-badge ${status.detailClass}">${esc(status.label)}</span></div></div>
        <div class="detail-row"><div class="detail-row-label">Schedule</div><div class="detail-row-value"><code>${esc(schedule)}</code></div></div>
        <div class="detail-row"><div class="detail-row-label">${esc(t('cron_next'))}</div><div class="detail-row-value">${esc(nextRun)}</div></div>
        <div class="detail-row"><div class="detail-row-label">${esc(t('cron_last'))}</div><div class="detail-row-value">${esc(lastRun)}</div></div>
        <div class="detail-row"><div class="detail-row-label">Deliver</div><div class="detail-row-value">${esc(deliver)}</div></div>
        <div class="detail-row"><div class="detail-row-label">${esc(t('cron_profile_label') || 'Profile')}</div><div class="detail-row-value"><span class="detail-badge active" title="${esc(profileTitle)}">${esc(profileLabel)}</span></div></div>
        <div class="detail-row"><div class="detail-row-label">Skills</div><div class="detail-row-value">${esc(skills)}</div></div>
        ${lastError}
      </div>
      <div class="detail-card">
        <div class="detail-card-title">Prompt</div>
        <div class="detail-prompt">${esc(job.prompt || '')}</div>
      </div>
      <div class="detail-card ${_cronNewJobIds.has(String(job.id)) ? 'has-new-run' : ''}" id="cronDetailRuns">
        <div class="detail-card-title">${esc(t('cron_last_output'))}</div>
        <div style="color:var(--muted);font-size:12px">${esc(t('loading'))}</div>
      </div>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _cronMode = 'read';
  _setCronHeaderButtons('read', job);
  // Load runs asynchronously
  _loadCronDetailRuns(job.id);
}

function _setCronHeaderButtons(mode, job) {
  const runBtn = $('btnRunTaskDetail');
  const pauseBtn = $('btnPauseTaskDetail');
  const resumeBtn = $('btnResumeTaskDetail');
  const editBtn = $('btnEditTaskDetail');
  const dupBtn = $('btnDuplicateTaskDetail');
  const delBtn = $('btnDeleteTaskDetail');
  const cancelBtn = $('btnCancelTaskDetail');
  const saveBtn = $('btnSaveTaskDetail');
  const hide = b => b && (b.style.display = 'none');
  const show = b => b && (b.style.display = '');
  if (mode === 'read') {
    show(runBtn);
    const status = job ? _cronStatusMeta(job) : null;
    const resumable = job && (
      job.state === 'paused' ||
      (status && (status.state === 'needs_attention' || status.state === 'schedule_error'))
    );
    if (resumable) { hide(pauseBtn); show(resumeBtn); }
    else { show(pauseBtn); hide(resumeBtn); }
    show(editBtn); show(dupBtn); show(delBtn); hide(cancelBtn); hide(saveBtn);
  } else if (mode === 'create' || mode === 'edit') {
    hide(runBtn); hide(pauseBtn); hide(resumeBtn); hide(editBtn); hide(dupBtn); hide(delBtn);
    show(cancelBtn); show(saveBtn);
  } else {
    [runBtn,pauseBtn,resumeBtn,editBtn,dupBtn,delBtn,cancelBtn,saveBtn].forEach(hide);
  }
}

async function _loadCronDetailRuns(jobId){
  try {
    const data = await api(`/api/crons/history?job_id=${encodeURIComponent(jobId)}&limit=50`);
    if (!_currentCronDetail || _currentCronDetail.id !== jobId) return;
    const card = $('cronDetailRuns');
    if (!card) return;
    if (!data.runs || !data.runs.length) {
      card.innerHTML = `<div class="detail-card-title">${esc(t('cron_last_output'))}</div><div style="color:var(--muted);font-size:12px">${esc(t('cron_no_runs_yet'))}</div>`;
      return;
    }
    const rows = data.runs.map((run, i) => {
      const ts = run.filename.replace('.md','').replace(/_/g,' ');
      const sizeStr = run.size > 1024 ? (run.size/1024).toFixed(1)+' KB' : run.size+' B';
      const dateStr = new Date(run.modified * 1000).toLocaleString();
      const rid = `cron-det-run-${jobId}-${i}`;
      return `<div class="detail-run-item" id="${rid}">
        <div class="detail-run-head" onclick="_loadRunContent('${esc(jobId)}','${esc(run.filename)}','${rid}')">
          <span><span style="opacity:.7">${esc(ts)}</span> <span style="opacity:.4;font-size:11px">${esc(sizeStr)}</span></span>
          <span style="opacity:.6">▸</span>
        </div>
        <div class="detail-run-body" style="color:var(--muted);font-size:12px">${esc(t('loading'))}</div>
      </div>`;
    }).join('');
    const countLabel = data.total > 50 ? ` (${data.total} runs, showing latest 50)` : ` (${data.total} runs)`;
    card.innerHTML = `<div class="detail-card-title">${esc(t('cron_last_output'))}${countLabel}</div>${rows}`;
  } catch(e) { /* ignore */ }
}

async function _loadRunContent(jobId, filename, runId){
  const body = document.querySelector(`#${runId} .detail-run-body`);
  if (!body) return;
  const item = document.getElementById(runId);
  if (!item.classList.contains('open')) {
    item.classList.add('open');
  }
  body.innerHTML = `<span style="opacity:.5">${esc(t('loading'))}</span>`;
  try {
    const data = await api(`/api/crons/run?job_id=${encodeURIComponent(jobId)}&filename=${encodeURIComponent(filename)}`);
    if (data.error) {
      body.textContent = data.error;
      return;
    }
    // Render markdown content using the same renderer as chat messages
    if (typeof renderMd === 'function') {
      body.innerHTML = renderMd(data.snippet || data.content);
    } else {
      body.textContent = data.snippet || data.content;
    }
    // Show "View full output" button if content was truncated
    if (data.content && data.snippet && data.content.length > data.snippet.length) {
      const btn = document.createElement('button');
      btn.style.cssText = 'margin-top:8px;padding:4px 12px;border-radius:var(--radius-btn);border:1px solid var(--border-subtle);background:var(--surface-subtle);color:var(--text-secondary);cursor:pointer;font-size:12px';
      btn.textContent = t('cron_view_full_output') || 'View full output';
      btn.onclick = () => {
        body.innerHTML = renderMd ? renderMd(data.content) : '';
        btn.remove();
      };
      body.appendChild(btn);
    }
  } catch(e) {
    body.textContent = 'Error: ' + e.message;
  }
}

function openCronDetail(id, el){
  const job = _cronList ? _cronList.find(j => j.id === id) : null;
  if (!job) return;
  document.querySelectorAll('.cron-item').forEach(e => e.classList.remove('active'));
  const target = el || $('cron-' + id);
  if (target) target.classList.add('active');
  // Remove new-run dot from this job since user is now viewing it
  _clearCronUnreadForJob(id);
  const dot = target && target.querySelector('.cron-new-dot');
  if (dot) dot.remove();
  _cronPreFormDetail = null;
  _editingCronId = null;
  _stopCronWatch();
  _renderCronDetail(job);
  _checkCronWatchOnDetail(id);
}

function _clearCronDetail(){
  _currentCronDetail = null;
  _cronMode = 'empty';
  _stopCronWatch();
  const title = $('taskDetailTitle');
  const body = $('taskDetailBody');
  const empty = $('taskDetailEmpty');
  if (title) title.textContent = '';
  if (body) { body.innerHTML = ''; body.style.display = 'none'; }
  if (empty) empty.style.display = '';
  _setCronHeaderButtons('empty');
}

async function runCurrentCron(){ if (_currentCronDetail) await cronRun(_currentCronDetail.id); }
async function pauseCurrentCron(){ if (_currentCronDetail) await cronPause(_currentCronDetail.id); }
async function resumeCurrentCron(){ if (_currentCronDetail) await cronResume(_currentCronDetail.id); }
async function copyCurrentCronDiagnostics(){
  if (!_currentCronDetail) return;
  try {
    await _copyText(_cronDiagnostics(_currentCronDetail));
    showToast(t('cron_diagnostics_copied'));
  } catch(e) { showToast(t('copy_failed'), 4000); }
}
function editCurrentCron(){
  if (!_currentCronDetail) return;
  openCronEdit(_currentCronDetail);
}
function duplicateCurrentCron(){
  if (!_currentCronDetail) return;
  const job = _currentCronDetail;
  if (typeof switchPanel === 'function' && _currentPanel !== 'tasks') switchPanel('tasks');
  _cronPreFormDetail = { ...job };
  _editingCronId = null;
  _cronMode = 'create';
  _cronIsDuplicate = true;
  _cronSelectedSkills = Array.isArray(job.skills) ? [...job.skills] : [];
  // Deduplicate name: append "(copy)", "(copy 2)", "(copy 3)" etc.
  const baseName = job.name || '';
  let dupName = baseName + ' (copy)';
  if (_cronList && _cronList.length) {
    const taken = new Set(_cronList.filter(j => j.name).map(j => j.name));
    if (taken.has(dupName)) {
      let n = 2;
      while (taken.has(baseName + ' (copy ' + n + ')')) n++;
      dupName = baseName + ' (copy ' + n + ')';
    }
  }
  _renderCronForm({
    name: dupName,
    schedule: job.schedule_display || (job.schedule && job.schedule.expression) || '',
    prompt: job.prompt || '',
    deliver: job.deliver || 'local',
    profile: job.profile || '',
    isEdit: false,
  });
  if (!_cronSkillsCache) {
    api('/api/skills').then(d=>{_cronSkillsCache=d.skills||[]; _bindCronSkillPicker();}).catch(()=>{});
  } else {
    _bindCronSkillPicker();
  }
}
async function deleteCurrentCron(){
  if (!_currentCronDetail) return;
  const id = _currentCronDetail.id;
  const _ok = await showConfirmDialog({title:t('cron_delete_confirm_title'),message:t('cron_delete_confirm_message'),confirmLabel:t('delete_title'),danger:true,focusCancel:true});
  if(!_ok) return;
  try {
    await api('/api/crons/delete', {method:'POST', body: JSON.stringify({job_id: id})});
    showToast(t('cron_job_deleted'));
    _clearCronDetail();
    await loadCrons();
  } catch(e) { showToast(t('delete_failed') + e.message, 4000); }
}

let _cronSelectedSkills=[];
let _cronIsDuplicate = false;
let _cronSkillsCache=null;
let _cronProfilesCache=null;

function openCronCreate(){
  if (typeof switchPanel === 'function' && _currentPanel !== 'tasks') switchPanel('tasks');
  _cronPreFormDetail = _currentCronDetail ? { ..._currentCronDetail } : null;
  _editingCronId = null;
  _cronMode = 'create';
  _cronIsDuplicate = false;
  _cronSelectedSkills = [];
  _renderCronForm({ name:'', schedule:'', prompt:'', deliver:'local', profile:'', isEdit:false });
  _cronSkillsCache = null;
  api('/api/skills').then(d=>{_cronSkillsCache=d.skills||[]; _bindCronSkillPicker();}).catch(()=>{});
  loadCronProfiles().then(()=>_refreshCronProfileSelect('')).catch(()=>{});
}

function openCronEdit(job){
  if (!job) return;
  _cronPreFormDetail = { ...job };
  _editingCronId = job.id;
  _cronMode = 'edit';
  _cronSelectedSkills = Array.isArray(job.skills) ? [...job.skills] : [];
  _renderCronForm({
    name: job.name || '',
    schedule: job.schedule_display || (job.schedule && job.schedule.expression) || '',
    prompt: job.prompt || '',
    deliver: job.deliver || 'local',
    profile: job.profile || '',
    isEdit: true,
  });
  if (!_cronSkillsCache) {
    api('/api/skills').then(d=>{_cronSkillsCache=d.skills||[]; _bindCronSkillPicker();}).catch(()=>{});
  } else {
    _bindCronSkillPicker();
  }
  loadCronProfiles().then(()=>_refreshCronProfileSelect(job.profile || '')).catch(()=>{});
}

function _renderCronForm({ name, schedule, prompt, deliver, profile, isEdit }){
  const title = $('taskDetailTitle');
  const body = $('taskDetailBody');
  const empty = $('taskDetailEmpty');
  if (!body || !title) return;
  title.textContent = isEdit ? (t('edit') + ' · ' + (name || schedule || t('scheduled_jobs'))) : t('new_job');
  const deliverOpt = (v,l) => `<option value="${v}"${deliver===v?' selected':''}>${esc(l)}</option>`;
  body.innerHTML = `
    <div class="main-view-content">
      <form class="detail-form" onsubmit="event.preventDefault(); saveCronForm();">
        <div class="detail-form-row">
          <label for="cronFormName">${esc(t('cron_name_label') || 'Name')}</label>
          <input type="text" id="cronFormName" value="${esc(name || '')}" placeholder="${esc(t('cron_name_placeholder') || 'Optional')}" autocomplete="off">
        </div>
        <div class="detail-form-row">
          <label for="cronFormSchedule">${esc(t('cron_schedule_label') || 'Schedule')}</label>
          <input type="text" id="cronFormSchedule" value="${esc(schedule || '')}" placeholder="0 9 * * *  —  every 1h  —  @daily" autocomplete="off" required>
          <div class="detail-form-hint">${esc(t('cron_schedule_hint') || "Cron expression or shorthand like 'every 1h'.")}</div>
        </div>
        <div class="detail-form-row">
          <label for="cronFormPrompt">${esc(t('cron_prompt_label') || 'Prompt')}</label>
          <textarea id="cronFormPrompt" rows="6" placeholder="${esc(t('cron_prompt_placeholder') || 'Must be self-contained')}" required>${esc(prompt || '')}</textarea>
        </div>
        <div class="detail-form-row">
          <label for="cronFormDeliver">${esc(t('cron_deliver_label') || 'Deliver output to')}</label>
          <select id="cronFormDeliver" ${isEdit ? 'disabled' : ''}>
            ${deliverOpt('local', t('cron_deliver_local') || 'Local (save output only)')}
            ${deliverOpt('discord','Discord')}
            ${deliverOpt('telegram','Telegram')}
          </select>
        </div>
        <div class="detail-form-row">
          <label for="cronFormProfile">${esc(t('cron_profile_label') || 'Profile')}</label>
          <select id="cronFormProfile">
            ${_cronProfileOptions(profile)}
          </select>
          <div class="detail-form-hint">${esc(t('cron_profile_server_default_hint') || 'Uses the WebUI server default profile at run time')}</div>
        </div>
        <div class="detail-form-row">
          <label for="cronFormSkillSearch">${esc(t('cron_skills_label') || 'Skills')}</label>
          <div class="skill-picker-wrap">
            <input type="text" id="cronFormSkillSearch" placeholder="${esc(t('cron_skills_placeholder') || 'Add skills (optional)...')}" autocomplete="off" ${isEdit ? 'disabled' : ''}>
            <div id="cronFormSkillDropdown" class="skill-picker-dropdown" style="display:none"></div>
            <div id="cronFormSkillTags" class="skill-picker-tags"></div>
          </div>
          ${isEdit ? `<div class="detail-form-hint">${esc(t('cron_skills_edit_hint') || 'Skill list is not editable after creation.')}</div>` : ''}
        </div>
        <div id="cronFormError" class="detail-form-error" style="display:none"></div>
      </form>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _setCronHeaderButtons(isEdit ? 'edit' : 'create');
  _renderCronSkillTags();
  const focusEl = $('cronFormName');
  if (focusEl) focusEl.focus();
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

function _bindCronSkillPicker(){
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
}

function cancelCronForm(){
  _editingCronId = null;
  if (_cronPreFormDetail) {
    const snap = _cronPreFormDetail;
    _cronPreFormDetail = null;
    _renderCronDetail(snap);
    return;
  }
  _cronPreFormDetail = null;
  _clearCronDetail();
}

async function saveCronForm(){
  const nameEl=$('cronFormName');
  const schEl=$('cronFormSchedule');
  const promptEl=$('cronFormPrompt');
  const delivEl=$('cronFormDeliver');
  const profileEl=$('cronFormProfile');
  const errEl=$('cronFormError');
  if(!schEl||!promptEl||!errEl) return;
  const name=(nameEl?nameEl.value:'').trim();
  const schedule=schEl.value.trim();
  const prompt=promptEl.value.trim();
  const deliver=delivEl?delivEl.value:'local';
  const profile=profileEl?profileEl.value:'';
  errEl.style.display='none';
  if(!schedule){errEl.textContent=t('cron_schedule_required_example');errEl.style.display='';return;}
  if(!prompt){errEl.textContent=t('cron_prompt_required');errEl.style.display='';return;}
  try{
    if (_editingCronId) {
      const updates = {job_id: _editingCronId, schedule, prompt, profile: profile};
      if (name) updates.name = name;
      await api('/api/crons/update', {method:'POST', body: JSON.stringify(updates)});
      const editedId = _editingCronId;
      _editingCronId = null;
      _cronPreFormDetail = null;
      showToast(t('cron_job_updated'));
      await loadCrons();
      const job = _cronList && _cronList.find(j => j.id === editedId);
      if (job) openCronDetail(editedId);
      return;
    }
    const body={schedule,prompt,deliver,profile: profile};
    if(_cronIsDuplicate) body.enabled=false;
    if(name)body.name=name;
    if(_cronSelectedSkills.length)body.skills=_cronSelectedSkills;
    const res = await api('/api/crons/create',{method:'POST',body:JSON.stringify(body)});
    _cronPreFormDetail = null;
    _cronIsDuplicate = false;
    showToast(t('cron_job_created'));
    await loadCrons();
    const newId = res && (res.id || (res.job && res.job.id));
    if (newId) openCronDetail(newId);
    else if (_cronList && _cronList.length) openCronDetail(_cronList[_cronList.length - 1].id);
  }catch(e){
    errEl.textContent=t('error_prefix')+e.message;errEl.style.display='';
  }
}

// Back-compat aliases for any stale callers
const submitCronCreate = saveCronForm;
function toggleCronForm(){ openCronCreate(); }

function _cronOutputSnippet(content) {
  // Extract the response body from a cron output .md file
  const lines = content.split('\n');
  const responseIdx = lines.findIndex(l => l.startsWith('## Response') || l.startsWith('# Response'));
  const body = (responseIdx >= 0 ? lines.slice(responseIdx + 1) : lines).join('\n').trim();
  return body.slice(0, 600) || '(empty)';
}

// ── Cron run watch ────────────────────────────────────────────────────────────
let _cronWatchInterval = null;
let _cronWatchStart = null;
let _cronWatchTimerInterval = null;

function _startCronWatch(jobId) {
  _stopCronWatch();
  _cronWatchStart = Date.now();
  _cronWatchInterval = setInterval(async () => {
    try {
      const data = await api(`/api/crons/status?job_id=${encodeURIComponent(jobId)}`);
      if (!data.running) {
        _stopCronWatch();
        if (_currentCronDetail && _currentCronDetail.id === jobId) {
          _loadCronDetailRuns(jobId);
        }
        return;
      }
      // Still running — update elapsed
      if (_currentCronDetail && _currentCronDetail.id === jobId) {
        const el = $('cronRunningIndicator');
        if (el) el.querySelector('.cron-watch-elapsed').textContent = _formatElapsed(data.elapsed);
      }
    } catch(e) { /* ignore poll errors */ }
  }, 3000);
  // Timer update every second
  _cronWatchTimerInterval = setInterval(() => {
    if (_currentCronDetail && _cronWatchStart) {
      const el = $('cronRunningIndicator');
      if (el) el.querySelector('.cron-watch-elapsed').textContent = _formatElapsed((Date.now() - _cronWatchStart) / 1000);
    }
  }, 1000);
  // Inject running indicator into detail card
  if (_currentCronDetail && _currentCronDetail.id === jobId) {
    _injectRunningIndicator();
  }
}

function _stopCronWatch() {
  if (_cronWatchInterval) { clearInterval(_cronWatchInterval); _cronWatchInterval = null; }
  if (_cronWatchTimerInterval) { clearInterval(_cronWatchTimerInterval); _cronWatchTimerInterval = null; }
  _cronWatchStart = null;
  const el = $('cronRunningIndicator');
  if (el) el.remove();
}

function _injectRunningIndicator() {
  const card = $('cronDetailRuns');
  if (!card || $('cronRunningIndicator')) return;
  const div = document.createElement('div');
  div.id = 'cronRunningIndicator';
  div.className = 'cron-running-indicator';
  div.innerHTML = `<span class="cron-watch-spinner"></span><span>${esc(t('cron_status_running'))}</span><span class="cron-watch-elapsed">0s</span>`;
  card.insertAdjacentElement('beforebegin', div);
}

function _formatElapsed(seconds) {
  if (seconds < 60) return Math.round(seconds) + 's';
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m + 'm ' + s + 's';
}

function _checkCronWatchOnDetail(jobId) {
  // When opening a detail view, check if job is running
  api(`/api/crons/status?job_id=${encodeURIComponent(jobId)}`).then(data => {
    if (data.running && _currentCronDetail && _currentCronDetail.id === jobId) {
      _startCronWatch(jobId);
    }
  }).catch(() => {});
}

async function cronRun(id) {
  try {
    await api('/api/crons/run', {method:'POST', body: JSON.stringify({job_id: id})});
    showToast(t('cron_job_triggered'));
    _startCronWatch(id);
  } catch(e) { showToast(t('failed_colon') + e.message, 4000); }
}

async function cronPause(id) {
  try {
    await api('/api/crons/pause', {method:'POST', body: JSON.stringify({job_id: id})});
    showToast(t('cron_job_paused'));
    await loadCrons();
  } catch(e) { showToast(t('failed_colon') + e.message, 4000); }
}

async function cronResume(id) {
  try {
    await api('/api/crons/resume', {method:'POST', body: JSON.stringify({job_id: id})});
    showToast(t('cron_job_resumed'));
    await loadCrons();
  } catch(e) { showToast(t('failed_colon') + e.message, 4000); }
}

let _editingCronId = null;

// ── Kanban panel (read-only) ──
function _kanbanColumnLabel(name){ return t('kanban_status_' + name) || name; }
function _kanbanTaskTitle(task){ return task.title || task.summary || task.id || t('kanban_task'); }
function _kanbanTaskBody(task){ return task.body || task.description || task.prompt || ''; }
function _kanbanTaskMeta(task){
  const bits = [];
  if (task.assignee) bits.push(task.assignee);
  if (task.tenant) bits.push(task.tenant);
  if (task.priority !== undefined && task.priority !== null) bits.push('P' + task.priority);
  if (task.comment_count) bits.push('💬 ' + task.comment_count);
  if (task.link_counts && task.link_counts.children) bits.push('↳ ' + task.link_counts.children);
  return bits;
}

function _kanbanCurrentFilters(){
  const q = $('kanbanSearch') ? $('kanbanSearch').value.trim().toLowerCase() : '';
  const assigneeEl = $('kanbanAssigneeFilter');
  const tenantEl = $('kanbanTenantFilter');
  const assignee = assigneeEl ? (assigneeEl.value || assigneeEl.dataset.defaultValue || '') : '';
  const tenant = tenantEl ? (tenantEl.value || tenantEl.dataset.defaultValue || '') : '';
  const includeArchived = !!($('kanbanIncludeArchived') && $('kanbanIncludeArchived').checked);
  const onlyMine = !!($('kanbanOnlyMine') && $('kanbanOnlyMine').checked);
  return {q, assignee, tenant, includeArchived, onlyMine};
}

function _kanbanApplyConfigDefaults(config){
  if (!config || _kanbanConfigApplied) return;
  if ($('kanbanTenantFilter') && config.default_tenant) $('kanbanTenantFilter').dataset.defaultValue = config.default_tenant;
  if ($('kanbanIncludeArchived') && config.include_archived_by_default === true) $('kanbanIncludeArchived').checked = true;
  if (config.lane_by_profile === true) _kanbanLanesByProfile = true;
  _kanbanConfigApplied = true;
}
let _kanbanConfigApplied = false;

function _kanbanSetSelectOptions(el, values, allLabelKey){
  if (!el) return;
  const current = el.value || el.dataset.defaultValue || '';
  const opts = [`<option value="">${esc(t(allLabelKey))}</option>`]
    .concat((values || []).map(v => `<option value="${esc(v)}">${esc(v)}</option>`));
  el.innerHTML = opts.join('');
  if ([...el.options].some(o => o.value === current)) el.value = current;
}

function _kanbanVisibleTasks(){
  const filters = _kanbanCurrentFilters();
  const columns = (_kanbanBoard && _kanbanBoard.columns) || [];
  return columns.map(col => {
    const tasks = (col.tasks || []).filter(task => {
      if (!filters.q) return true;
      const haystack = [task.id, _kanbanTaskTitle(task), _kanbanTaskBody(task), task.assignee, task.tenant]
        .filter(Boolean).join(' ').toLowerCase();
      return haystack.includes(filters.q);
    });
    return {...col, tasks};
  });
}

function _kanbanRenderSidebar(columns){
  const list = $('kanbanList');
  if (!list) return;
  const tasks = columns.flatMap(col => (col.tasks || []).map(task => ({...task, status: task.status || col.name})));
  if (!tasks.length) {
    list.innerHTML = `<div class="kanban-empty" data-i18n="kanban_no_matching_tasks">${esc(t('kanban_no_matching_tasks'))}</div>`;
    return;
  }
  list.innerHTML = tasks.map(task => {
    const meta = _kanbanTaskMeta(task);
    return `<button class="kanban-list-item" onclick="loadKanbanTask('${esc(task.id)}')">
      <span class="kanban-list-status">${esc(_kanbanColumnLabel(task.status))}</span>
      <span class="kanban-list-title">${esc(_kanbanTaskTitle(task))}</span>
      ${meta.length ? `<span class="kanban-meta">${esc(meta.join(' · '))}</span>` : ''}
    </button>`;
  }).join('');
}


function _kanbanRenderMarkdownInline(escaped){
  return String(escaped || '')
    .replace(/`([^`\n]+)`/g, (_m, code) => `<code>${code}</code>`)
    .replace(/\*\*([^*\n]+)\*\*/g, (_m, text) => `<strong>${text}</strong>`)
    .replace(/(^|[^*])\*([^*\n]+)\*/g, (_m, prefix, text) => `${prefix}<em>${text}</em>`)
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g, (_m, text, href) => `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`);
}

function _kanbanRenderMarkdown(source){
  if (!source) return '';
  return `<div class="hermes-kanban-md">${esc(source).split(/\r?\n/).map(line => line.trim() ? `<p>${_kanbanRenderMarkdownInline(line)}</p>` : '').join('')}</div>`;
}

function _kanbanFormatDuration(seconds){
  const n = Number(seconds);
  if (!Number.isFinite(n) || n <= 0) return '';
  if (n < 60) return Math.round(n) + 's';
  if (n < 3600) return Math.round(n / 60) + 'm';
  if (n < 86400) return Math.round(n / 3600) + 'h';
  return Math.round(n / 86400) + 'd';
}

function _kanbanTaskAge(task){
  const age = task && (task.age_seconds || task.age);
  if (Number.isFinite(Number(age))) return _kanbanFormatDuration(age);
  return '';
}

function _kanbanCardStalenessClass(task){
  const age = Number(task && (task.age_seconds || task.age));
  const status = task && task.status;
  if (!Number.isFinite(age)) return '';
  if ((status === 'running' && age > 3600) || (status === 'blocked' && age > 86400)) return 'kanban-card-stale-red';
  if ((status === 'running' && age > 600) || (status === 'ready' && age > 3600) || (status === 'blocked' && age > 3600)) return 'kanban-card-stale-amber';
  return '';
}

function _kanbanCardQuickActions(task){
  const id = esc(task.id || '');
  const status = task.status || '';
  const start = status !== 'running' && status !== 'done' && status !== 'archived' ? `<button type="button" class="kanban-card-action" onclick="quickKanbanCardAction(event,'${id}','running')">${esc(t('kanban_card_start'))}</button>` : '';
  const complete = status !== 'done' && status !== 'archived' ? `<button type="button" class="kanban-card-action" onclick="quickKanbanCardAction(event,'${id}','done')">${esc(t('kanban_card_complete'))}</button>` : '';
  const archive = status !== 'archived' ? `<button type="button" class="kanban-card-action danger" onclick="quickKanbanCardAction(event,'${id}','archived')">${esc(t('kanban_card_archive'))}</button>` : '';
  return `<div class="kanban-card-actions" onclick="event.stopPropagation()">${start}${complete}${archive}</div>`;
}

async function quickKanbanCardAction(event, taskId, status){
  if (event) event.stopPropagation();
  return updateKanbanTask(taskId, {status});
}

function dragKanbanTask(event, taskId){
  if (!event.dataTransfer) return;
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', taskId);
}

function allowKanbanDrop(event){
  // Don't accept drops into the 'running' column. Entering 'running' is owned
  // by the dispatcher/claim_task path (sets claim_lock + claim_expires +
  // started_at + worker_pid). A drag-drop would bypass that contract and the
  // bridge would reject the resulting PATCH with HTTP 400 anyway. Refuse the
  // drop visually so users see immediate feedback.
  const target = event.currentTarget;
  if (target && target.dataset && target.dataset.kanbanStatus === 'running') {
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'none';
    return;
  }
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
}

function clearKanbanDrop(event){
  if (event && event.currentTarget) event.currentTarget.classList.remove('drop-target');
}

async function dropKanbanTask(event, status){
  event.preventDefault();
  clearKanbanDrop(event);
  const taskId = event.dataTransfer ? event.dataTransfer.getData('text/plain') : '';
  if (taskId && status) await updateKanbanTask(taskId, {status});
}

function _kanbanLaneNames(columns){
  const names = new Set();
  columns.forEach(col => (col.tasks || []).forEach(task => names.add(task.assignee || t('kanban_unassigned'))));
  return Array.from(names).sort((a, b) => String(a).localeCompare(String(b)));
}

function _kanbanRenderColumn(col){
  const tasks = col.tasks || [];
  return `<section class="kanban-column" data-status="${esc(col.name)}" data-kanban-status="${esc(col.name)}" ondragover="allowKanbanDrop(event)" ondragenter="event.currentTarget.classList.add('drop-target')" ondragleave="clearKanbanDrop(event)" ondrop="dropKanbanTask(event, '${esc(col.name)}')">
      <div class="kanban-column-head">
        <span>${esc(_kanbanColumnLabel(col.name))}</span>
        <span class="kanban-count">${tasks.length}</span>
      </div>
      <div class="kanban-column-body">
        ${tasks.length ? tasks.map(task => _kanbanCard(task, col.name)).join('') : `<div class="kanban-empty">${esc(t('kanban_empty'))}</div>`}
      </div>
    </section>`;
}

function _kanbanRenderProfileLanes(columns){
  const lanes = _kanbanLaneNames(columns);
  if (!lanes.length) return columns.map(_kanbanRenderColumn).join('');
  return `<div class="kanban-profile-lanes">${lanes.map(lane => {
    const laneCols = columns.map(col => ({...col, tasks: (col.tasks || []).filter(task => (task.assignee || t('kanban_unassigned')) === lane)}));
    const count = laneCols.reduce((sum, col) => sum + (col.tasks || []).length, 0);
    return `<section class="kanban-profile-lane" data-kanban-lane="${esc(lane)}"><header class="kanban-profile-lane-head"><span>${esc(lane)}</span><span class="kanban-count">${count}</span></header><div class="kanban-board kanban-board-in-lane">${laneCols.map(_kanbanRenderColumn).join('')}</div></section>`;
  }).join('')}</div>`;
}

function _kanbanRenderBoard(){
  const board = $('kanbanBoard');
  if (!board) return;
  if (!_kanbanBoard || !_kanbanBoard.columns) {
    board.innerHTML = `<div class="main-view-empty"><div class="main-view-empty-title">${esc(t('kanban_no_data'))}</div></div>`;
    return;
  }
  const columns = _kanbanVisibleTasks();
  const total = columns.reduce((n, col) => n + (col.tasks || []).length, 0);
  if ($('kanbanSummary')) $('kanbanSummary').textContent = String(t('kanban_visible_tasks')).replace('{0}', total);
  _kanbanRenderSidebar(columns);
  board.innerHTML = _kanbanLanesByProfile ? _kanbanRenderProfileLanes(columns) : columns.map(_kanbanRenderColumn).join('');
}

function _kanbanCard(task, status){
  const priority = Number(task.priority || 0);
  const links = task.link_counts || {};
  const linkTotal = Number(links.parents || 0) + Number(links.children || 0);
  const comments = Number(task.comment_count || 0);
  const age = _kanbanTaskAge(task);
  const stale = _kanbanCardStalenessClass(task);
  const body = _kanbanTaskBody(task);
  const assignee = task.assignee ? `<span class="kanban-card-assignee">@${esc(task.assignee)}</span>` : `<span class="kanban-card-unassigned">${esc(t('kanban_unassigned'))}</span>`;
  return `<article class="kanban-card ${esc(stale)}" data-kanban-task-id="${esc(task.id)}" draggable="true" ondragstart="dragKanbanTask(event, '${esc(task.id)}')" onclick="loadKanbanTask('${esc(task.id)}')" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();loadKanbanTask('${esc(task.id)}')}">
    <div class="kanban-card-topline"><span class="kanban-card-id">${esc(task.id || '')}</span>${priority ? `<span class="kanban-badge priority">P${priority}</span>` : ''}${task.tenant ? `<span class="kanban-badge tenant">${esc(task.tenant)}</span>` : ''}</div>
    <div class="kanban-card-title">${esc(_kanbanTaskTitle(task))}</div>
    ${body ? `<div class="kanban-card-body">${_kanbanRenderMarkdown(body)}</div>` : ''}
    <div class="kanban-card-meta">${assignee}${comments ? `<span class="kanban-card-metric">💬 ${comments}</span>` : ''}${linkTotal ? `<span class="kanban-card-metric">↔ ${linkTotal}</span>` : ''}${age ? `<span class="kanban-card-age">${esc(age)}</span>` : ''}</div>
    ${_kanbanCardQuickActions(task)}
  </article>`;
}

async function loadKanban(animate){
  const board = $('kanbanBoard');
  const list = $('kanbanList');
  try {
    if (animate && board) board.innerHTML = `<div style="padding:16px;color:var(--muted);font-size:13px">${esc(t('loading'))}</div>`;
    const config = await api('/api/kanban/config' + _kanbanBoardQuery());
    let assignees = null;
    try { assignees = await api('/api/kanban/assignees' + _kanbanBoardQuery()); } catch(e) { assignees = null; }
    _kanbanApplyConfigDefaults(config);
    const filters = _kanbanCurrentFilters();
    const params = new URLSearchParams();
    if (filters.assignee) params.set('assignee', filters.assignee);
    if (filters.tenant) params.set('tenant', filters.tenant);
    if (filters.includeArchived) params.set('include_archived', '1');
    if (filters.onlyMine) params.set('only_mine', '1');
    if (_kanbanCurrentBoard) params.set('board', _kanbanCurrentBoard);
    const path = '/api/kanban/board' + (params.toString() ? '?' + params.toString() : '');
    const data = await api(path);
    if (data && data.changed === false && _kanbanBoard) { _kanbanRenderBoard(); return; }
    _kanbanBoard = data || {columns: []};
    if ((!_kanbanBoard.columns || !_kanbanBoard.columns.length) && config && config.columns) {
      _kanbanBoard.columns = config.columns.map(name => ({name, tasks: []}));
    }
    _kanbanLatestEventId = Number(_kanbanBoard.latest_event_id || 0);
    // Toggle the "Read-only view" banner based on the bridge's read_only flag.
    // Bridge sets read_only=true only when the kanban_db connection cannot accept
    // writes (e.g. dispatcher contention or library missing). Hide otherwise.
    try {
      const ro = document.querySelector('.kanban-readonly');
      if (ro) ro.style.display = _kanbanBoard.read_only ? '' : 'none';
    } catch(_) {}
    _kanbanSetSelectOptions($('kanbanAssigneeFilter'), _kanbanBoard.assignees || (assignees && assignees.assignees) || (config && config.assignees), 'kanban_all_assignees');
    _kanbanSetSelectOptions($('kanbanTenantFilter'), _kanbanBoard.tenants, 'kanban_all_tenants');
    await loadKanbanStats();
    // Refresh the multi-board switcher (and resolve which board to show
    // from localStorage / server state). Best-effort — failures hide the
    // switcher rather than blocking the panel from rendering.
    await loadKanbanBoards();
    _kanbanStartPolling();
    _kanbanRenderBoard();
  } catch(e) {
    const msg = `${esc(t('kanban_unavailable'))}: ${esc(e.message || e)}`;
    if (board) board.innerHTML = `<div class="main-view-empty"><div class="main-view-empty-title">${msg}</div></div>`;
    if (list) list.innerHTML = `<div class="kanban-empty">${msg}</div>`;
  }
}

function filterKanban(){ _kanbanRenderBoard(); }

async function loadKanbanStats(){
  try {
    const stats = await api('/api/kanban/stats' + _kanbanBoardQuery());
    const el = $('kanbanStats');
    if (!el) return;
    const byStatus = (stats && stats.by_status) || {};
    const total = Object.values(byStatus).reduce((a, b) => a + Number(b || 0), 0);
    const cells = Object.entries(byStatus).sort(([a], [b]) => a.localeCompare(b)).map(([status, count]) =>
      `<span class="kanban-stat-cell"><strong>${esc(String(count))}</strong> ${esc(_kanbanColumnLabel(status))}</span>`
    ).join('');
    el.innerHTML = `<div class="kanban-stats-grid"><span class="kanban-stat-cell total"><strong>${esc(String(total))}</strong> ${esc(t('kanban_stats'))}</span>${cells}</div>`;
  } catch(e) { /* stats are best-effort */ }
}

async function refreshKanbanEvents(){
  if (_currentPanel !== 'kanban' || !_kanbanLatestEventId) return;
  try {
    const eventsEndpoint = '/api/kanban/events';
    const events = await api(eventsEndpoint + _kanbanBoardQuery({since: _kanbanLatestEventId}));
    if (events && Array.isArray(events.events) && events.events.length) {
      _kanbanLatestEventId = Number(events.latest_event_id || events.cursor || _kanbanLatestEventId);
      await loadKanban(true);
      if (_kanbanCurrentTaskId && events.events.some(ev => ev.task_id === _kanbanCurrentTaskId)) await loadKanbanTask(_kanbanCurrentTaskId);
    }
  } catch(e) { /* polling should not spam toasts */ }
}

function _kanbanStartPolling(){
  // Prefer SSE for low-latency live updates. Fall back to polling on
  // browsers without EventSource or after repeated stream failures.
  if (typeof EventSource === 'undefined' || _kanbanEventSourceFailures >= 3) {
    if (_kanbanPollTimer) return;
    _kanbanPollTimer = setInterval(refreshKanbanEvents, 30000);
    return;
  }
  _kanbanStartEventStream();
}

function _kanbanStopPolling(){
  if (_kanbanPollTimer) { clearInterval(_kanbanPollTimer); _kanbanPollTimer = null; }
  if (_kanbanEventSource) { try { _kanbanEventSource.close(); } catch(_) {} _kanbanEventSource = null; }
}

function _kanbanStartEventStream(){
  // Tear down any prior stream before opening a new one (board switch,
  // login change, etc.).
  if (_kanbanEventSource) { try { _kanbanEventSource.close(); } catch(_) {} _kanbanEventSource = null; }
  const since = Number(_kanbanLatestEventId || 0);
  let url = '/api/kanban/events/stream' + _kanbanBoardQuery({since: since});
  let es;
  try {
    es = new EventSource(url);
  } catch(e) {
    _kanbanEventSourceFailures += 1;
    if (_kanbanEventSourceFailures < 3 && !_kanbanPollTimer) {
      _kanbanPollTimer = setInterval(refreshKanbanEvents, 30000);
    }
    return;
  }
  _kanbanEventSource = es;
  es.addEventListener('hello', (ev) => {
    // Reset the failure counter on a successful handshake.
    _kanbanEventSourceFailures = 0;
  });
  es.addEventListener('events', async (ev) => {
    if (_currentPanel !== 'kanban') return;  // ignore while user is on another panel
    let data;
    try { data = JSON.parse(ev.data); } catch(_) { return; }
    if (!data || !Array.isArray(data.events) || !data.events.length) return;
    _kanbanLatestEventId = Number(data.cursor || _kanbanLatestEventId);
    // Re-fetch the board so the visual state reflects the new events.
    // Throttle: if events are arriving faster than ~1/sec we coalesce.
    _scheduleKanbanRefresh(data.events);
  });
  es.onerror = () => {
    _kanbanEventSourceFailures += 1;
    if (_kanbanEventSourceFailures >= 3) {
      // Give up on SSE for this session — fall back to HTTP polling.
      try { es.close(); } catch(_) {}
      _kanbanEventSource = null;
      if (!_kanbanPollTimer) _kanbanPollTimer = setInterval(refreshKanbanEvents, 30000);
    }
    // EventSource auto-reconnects under the hood; nothing more to do here
    // until we hit the failure limit.
  };
}

let _kanbanRefreshScheduled = false;
let _kanbanRefreshPendingTaskIds = new Set();
function _scheduleKanbanRefresh(events){
  for (const ev of events) {
    if (ev && ev.task_id) _kanbanRefreshPendingTaskIds.add(ev.task_id);
  }
  if (_kanbanRefreshScheduled) return;
  _kanbanRefreshScheduled = true;
  // 250ms debounce — keeps a burst of N events from triggering N reloads.
  setTimeout(async () => {
    _kanbanRefreshScheduled = false;
    const taskIds = Array.from(_kanbanRefreshPendingTaskIds);
    _kanbanRefreshPendingTaskIds.clear();
    if (_currentPanel !== 'kanban') return;
    try {
      await loadKanban(true);
      if (_kanbanCurrentTaskId && taskIds.includes(_kanbanCurrentTaskId)) {
        await loadKanbanTask(_kanbanCurrentTaskId);
      }
    } catch(_) { /* swallow — SSE refresh shouldn't toast */ }
  }, 250);
}

// Build a "?board=<slug>" or "?since=N&board=<slug>" query string fragment
// based on the active board. Empty when the user is on the default board
// AND nobody has explicitly switched (so we don't pin to "default" and
// override a hypothetical server-side switch).
function _kanbanBoardQuery(extra){
  const params = new URLSearchParams();
  if (extra) {
    for (const [k, v] of Object.entries(extra)) {
      if (v !== null && v !== undefined && v !== '') params.set(k, String(v));
    }
  }
  if (_kanbanCurrentBoard) params.set('board', _kanbanCurrentBoard);
  const s = params.toString();
  return s ? '?' + s : '';
}

async function nudgeKanbanDispatcher(){
  try {
    const dispatchEndpoint = '/api/kanban/dispatch';
    await api(dispatchEndpoint + '?dry_run=1&max=1' + (_kanbanCurrentBoard ? '&board=' + encodeURIComponent(_kanbanCurrentBoard) : ''), {method: 'POST'});
    showToast(t('kanban_nudge_dispatcher'));
    await loadKanban(true);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

function _kanbanSelectedTaskIds(){
  const selected = Array.from(document.querySelectorAll('.kanban-card.selected')).map(card => card.dataset.kanbanTaskId).filter(Boolean);
  return selected.length ? selected : (_kanbanCurrentTaskId ? [_kanbanCurrentTaskId] : []);
}

async function bulkUpdateKanban(){
  const ids = _kanbanSelectedTaskIds();
  const status = $('kanbanBulkStatus') ? $('kanbanBulkStatus').value : '';
  if (!ids.length || !status) return;
  try {
    await api('/api/kanban/tasks/bulk' + _kanbanBoardQuery(), {method: 'POST', body: JSON.stringify({ids, status})});
    showToast(t('kanban_bulk_action'));
    await loadKanban(true);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

async function blockKanbanTask(taskId){
  try {
    await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + '/block' + _kanbanBoardQuery(), {method: 'POST', body: JSON.stringify({reason: 'blocked from WebUI'})});
    await loadKanbanTask(taskId);
    await loadKanban(true);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

async function unblockKanbanTask(taskId){
  try {
    await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + '/unblock' + _kanbanBoardQuery(), {method: 'POST', body: JSON.stringify({})});
    await loadKanbanTask(taskId);
    await loadKanban(true);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

function closeKanbanTaskDetail(){
  _kanbanCurrentTaskId = null;
  const preview = $('kanbanTaskPreview');
  if (preview) {
    preview.style.display = 'none';
    preview.innerHTML = '';
  }
  const board = $('kanbanBoard');
  if (board) board.querySelectorAll('.kanban-card').forEach(card => card.classList.remove('selected'));
}

function _kanbanFormatTimestamp(value){
  if (value === undefined || value === null || value === '') return '';
  let date = null;
  if (typeof value === 'number') date = new Date(value > 100000000000 ? value : value * 1000);
  else if (/^\d+(?:\.\d+)?$/.test(String(value).trim())) {
    const n = Number(value);
    date = new Date(n > 100000000000 ? n : n * 1000);
  } else {
    date = new Date(value);
  }
  if (!date || Number.isNaN(date.getTime())) return String(value);
  try { return date.toLocaleString(); } catch(e) { return date.toISOString(); }
}

function _kanbanEventSummary(event){
  const kind = event.kind || event.type || 'event';
  const payload = event.payload || event.data || {};
  if (payload && typeof payload === 'object') {
    const parts = [];
    if (payload.status) parts.push(String(payload.status));
    if (payload.reason) parts.push(String(payload.reason));
    if (payload.summary) parts.push(String(payload.summary));
    if (payload.fields && Array.isArray(payload.fields)) parts.push(payload.fields.join(', '));
    if (parts.length) return `${kind}: ${parts.join(' · ')}`;
  }
  return String(kind);
}

function _kanbanFormatDetailValue(value){
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'object') {
    try { return JSON.stringify(value, null, 2); } catch(e) { return String(value); }
  }
  return String(value);
}

function _kanbanDetailSection(cls, title, inner, emptyKey){
  const content = inner || `<div class="kanban-detail-empty">${esc(t(emptyKey))}</div>`;
  return `<section class="kanban-detail-section ${cls}">
    <h3>${esc(title)}</h3>
    ${content}
  </section>`;
}

function _kanbanCommentHtml(comment){
  const body = comment.body || comment.text || comment.content || '';
  const by = comment.author || comment.created_by || comment.actor || '';
  const at = _kanbanFormatTimestamp(comment.created_at || comment.ts || '');
  return `<div class="kanban-detail-row">
    <div class="kanban-detail-row-main">${esc(body)}</div>
    <div class="kanban-detail-row-meta">${esc([by, at].filter(Boolean).join(' · '))}</div>
  </div>`;
}

function _kanbanEventHtml(event){
  const at = _kanbanFormatTimestamp(event.created_at || event.ts || '');
  const payload = _kanbanFormatDetailValue(event.payload || event.data || '');
  return `<div class="kanban-detail-row">
    <div class="kanban-detail-row-main">${esc(_kanbanEventSummary(event))}</div>
    ${payload ? `<pre class="kanban-detail-pre">${esc(payload)}</pre>` : ''}
    <div class="kanban-detail-row-meta">${esc(at)}</div>
  </div>`;
}

function _kanbanRunHtml(run){
  const status = run.status || run.state || run.result || '';
  const label = run.run_id || run.id || run.worker || t('kanban_task');
  const started = _kanbanFormatTimestamp(run.started_at || run.created_at || '');
  const finished = _kanbanFormatTimestamp(run.finished_at || run.completed_at || '');
  const detail = run.error || run.summary || run.log_tail || '';
  return `<div class="kanban-detail-row">
    <div class="kanban-detail-row-main">${esc(label)}${status ? ` · ${esc(status)}` : ''}</div>
    ${detail ? `<pre class="kanban-detail-pre">${esc(_kanbanFormatDetailValue(detail))}</pre>` : ''}
    <div class="kanban-detail-row-meta">${esc([started, finished].filter(Boolean).join(' → '))}</div>
  </div>`;
}

function _kanbanLinksHtml(links){
  const parents = (links && links.parents) || [];
  const children = (links && links.children) || [];
  if (!parents.length && !children.length) return '';
  const item = id => `<code>${esc(id)}</code>`;
  return `<div class="kanban-detail-links-grid">
    <div><strong>${esc(t('kanban_parents'))}</strong><div>${parents.length ? parents.map(item).join(' ') : esc(t('kanban_empty'))}</div></div>
    <div><strong>${esc(t('kanban_children'))}</strong><div>${children.length ? children.map(item).join(' ') : esc(t('kanban_empty'))}</div></div>
  </div>`;
}

async function createKanbanTask(){
  const input = document.getElementById('kanbanNewTaskTitle');
  const title = input ? input.value.trim() : '';
  if (!title) return;
  try {
    const created = await api('/api/kanban/tasks' + _kanbanBoardQuery(), {
      method: 'POST',
      body: JSON.stringify({title}),
    });
    if (input) input.value = '';
    await loadKanban(true);
    if (created && created.task && created.task.id) await loadKanbanTask(created.task.id);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

async function updateKanbanTask(taskId, patch){
  if (!taskId || !patch) return;
  try {
    const updated = await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + _kanbanBoardQuery(), {
      method: 'PATCH',
      body: JSON.stringify(patch),
    });
    await loadKanban(true);
    await loadKanbanTask((updated && updated.task && updated.task.id) || taskId);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

async function addKanbanComment(taskId){
  const input = document.getElementById('kanbanCommentInput');
  const body = input ? input.value.trim() : '';
  if (!taskId || !body) return;
  try {
    await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + '/comments' + _kanbanBoardQuery(), {
      method: 'POST',
      body: JSON.stringify({body}),
    });
    if (input) input.value = '';
    await loadKanbanTask(taskId);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

function _kanbanRenderTaskDetail(data){
  const task = data.task || {};
  const log = data.log || {};
  const title = _kanbanTaskTitle(task);
  const body = _kanbanTaskBody(task) || t('kanban_no_description');
  const meta = _kanbanTaskMeta(task);
  const comments = data.comments || [];
  const events = data.events || [];
  const links = data.links || {};
  const runs = data.runs || [];
  // Note: 'running' is intentionally absent — entering 'running' is the
  // dispatcher/claim_task path's responsibility, not a user UI write. The
  // bridge rejects PATCH status='running' with HTTP 400 to match the agent
  // dashboard plugin's contract. UI users want to claim/promote a ready task
  // via the dispatcher Nudge button, not flip it to running by hand.
  const statusButtons = ['triage', 'todo', 'ready', 'blocked', 'done', 'archived'].map(status =>
    `<button class="btn secondary" onclick="updateKanbanTask('${esc(task.id)}',{status:'${status}'})">${esc(_kanbanColumnLabel(status))}</button>`
  ).join('') + `<button class="btn secondary" onclick="blockKanbanTask('${esc(task.id)}')">${esc(t('kanban_block'))}</button><button class="btn secondary" onclick="unblockKanbanTask('${esc(task.id)}')">${esc(t('kanban_unblock'))}</button>`;
  return `<div class="kanban-task-preview-header">
      <button class="btn secondary kanban-back-btn" onclick="closeKanbanTaskDetail()">${esc(t('kanban_back_to_board'))}</button>
      <div class="kanban-task-preview-title">${esc(title)}</div>
    </div>
    <div class="kanban-task-preview-body">${esc(body)}</div>
    ${meta.length ? `<div class="kanban-meta">${esc(meta.join(' · '))}</div>` : ''}
    <div class="kanban-status-actions">${statusButtons}</div>
    <div class="kanban-detail-grid">
      ${_kanbanDetailSection('kanban-detail-comments', String(t('kanban_comments_count')).replace('{0}', comments.length), comments.map(_kanbanCommentHtml).join(''), 'kanban_no_comments')}
      ${_kanbanDetailSection('kanban-detail-events', String(t('kanban_events_count')).replace('{0}', events.length), events.map(_kanbanEventHtml).join(''), 'kanban_no_events')}
      ${_kanbanDetailSection('kanban-detail-links', t('kanban_links'), _kanbanLinksHtml(links), 'kanban_empty')}
      ${_kanbanDetailSection('kanban-detail-runs', String(t('kanban_runs_count')).replace('{0}', runs.length), runs.map(_kanbanRunHtml).join(''), 'kanban_no_runs')}
      ${_kanbanDetailSection('kanban-detail-log', t('kanban_worker_log'), log.content ? `<pre class="kanban-detail-pre">${esc(log.content)}</pre>` : '', 'kanban_empty')}
    </div>
    <div class="kanban-comment-form">
      <textarea id="kanbanCommentInput" rows="2" placeholder="${esc(t('kanban_add_comment'))}"></textarea>
      <button class="btn primary" onclick="addKanbanComment('${esc(task.id)}')">${esc(t('kanban_add_comment'))}</button>
    </div>`;
}

async function loadKanbanTask(taskId){
  if (!taskId) return;
  try {
    const data = await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + _kanbanBoardQuery());
    const logEndpoint = '/api/kanban/tasks/' + encodeURIComponent(taskId) + '/log' + _kanbanBoardQuery();
    try { data.log = await api(logEndpoint + '?tail=65536'); } catch(e) { data.log = {}; }
    _kanbanCurrentTaskId = taskId;
    const task = data.task || {};
    const title = _kanbanTaskTitle(task);
    const board = $('kanbanBoard');
    if (board) {
      board.querySelectorAll('.kanban-card').forEach(card => card.classList.remove('selected'));
      Array.from(board.querySelectorAll('.kanban-card')).find(card => card.dataset.kanbanTaskId === taskId)?.classList.add('selected');
    }
    const preview = $('kanbanTaskPreview');
    if (preview) {
      preview.style.display = '';
      preview.innerHTML = _kanbanRenderTaskDetail(data);
    }
    showToast(`${t('kanban_task')}: ${title}`);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

function loadTodos() {
  const panel = $('todoPanel');
  if (!panel) return;
  const sourceMessages = (S.session && Array.isArray(S.session.messages) && S.session.messages.length) ? S.session.messages : S.messages;
  // Parse the most recent todo state from message history
  let todos = [];
  for (let i = sourceMessages.length - 1; i >= 0; i--) {
    const m = sourceMessages[i];
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
    panel.innerHTML = `<div style="color:var(--muted);font-size:12px;padding:4px 0">${esc(t('todos_no_active'))}</div>`;
    return;
  }
  const statusIcon = {pending:li('square',14), in_progress:li('loader',14), completed:li('check',14), cancelled:li('x',14)};
  const statusColor = {pending:'var(--muted)', in_progress:'var(--blue)', completed:'rgba(100,200,100,.8)', cancelled:'rgba(200,100,100,.5)'};
  panel.innerHTML = todos.map(t => `
    <div style="display:flex;align-items:flex-start;gap:10px;padding:6px 0;border-bottom:1px solid var(--border);">
      <span style="font-size:14px;display:inline-flex;align-items:center;flex-shrink:0;margin-top:1px;color:${statusColor[t.status]||'var(--muted)'}">${statusIcon[t.status]||li('square',14)}</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;color:${t.status==='completed'?'var(--muted)':t.status==='in_progress'?'var(--text)':'var(--text)'};${t.status==='completed'?'text-decoration:line-through;opacity:.5':''};line-height:1.4">${esc(t.content)}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:2px;opacity:.6">${esc(t.id)} · ${esc(t.status)}</div>
      </div>
    </div>`).join('');
}

// ────────────────────────────────────────────────────────────────────────────
// Kanban: multi-board switcher + create/rename/archive modal
// ────────────────────────────────────────────────────────────────────────────
//
// The bridge exposes /api/kanban/boards (GET/POST), /boards/<slug>
// (PATCH/DELETE), and /boards/<slug>/switch (POST). The UI surfaces these
// as a "Default ▾" dropdown next to the Board title — clicking it opens
// a menu listing every board (current first, with task counts), plus
// actions to create / rename / archive.

const KANBAN_BOARD_LS_KEY = 'hermes-kanban-active-board';

function _kanbanGetSavedBoard(){
  try { return localStorage.getItem(KANBAN_BOARD_LS_KEY) || null; } catch(_) { return null; }
}

function _kanbanSetSavedBoard(slug){
  try {
    if (slug && slug !== 'default') localStorage.setItem(KANBAN_BOARD_LS_KEY, slug);
    else localStorage.removeItem(KANBAN_BOARD_LS_KEY);
  } catch(_) {}
}

async function loadKanbanBoards(){
  // Fetches the boards list and updates the switcher UI. Best-effort —
  // failures hide the switcher rather than blocking the panel from rendering.
  const switcher = document.getElementById('kanbanBoardSwitcher');
  if (!switcher) return;
  let data;
  try {
    data = await api('/api/kanban/boards');
  } catch(e) {
    // Hide switcher on error so the user isn't stuck with a half-broken UI.
    switcher.hidden = true;
    return;
  }
  const boards = (data && data.boards) || [];
  const serverCurrent = (data && data.current) || 'default';
  _kanbanBoardsList = boards;
  // Resolution chain for the active board:
  //   localStorage hint → server's `current` → 'default'.
  // The localStorage hint is honoured ONLY if it points at a board that
  // still exists; otherwise we fall back to the server's pointer.
  const saved = _kanbanGetSavedBoard();
  let active = serverCurrent;
  if (saved && boards.some(b => b.slug === saved)) {
    active = saved;
  }
  _kanbanCurrentBoard = (active === 'default') ? null : active;
  // The switcher is visible whenever ≥1 non-default board exists OR the
  // current board is non-default. (If you only have 'default', a switcher
  // adds clutter without value.)
  const hasMultiple = boards.length > 1 || (active !== 'default');
  switcher.hidden = !hasMultiple;
  if (!hasMultiple) return;
  // Update the toggle label/icon
  const activeMeta = boards.find(b => b.slug === active) || {slug: active, name: active, icon: '', color: ''};
  const nameEl = document.getElementById('kanbanBoardSwitcherName');
  const iconEl = document.getElementById('kanbanBoardSwitcherIcon');
  if (nameEl) nameEl.textContent = activeMeta.name || activeMeta.slug || 'Default';
  if (iconEl) {
    iconEl.textContent = activeMeta.icon || '';
    if (activeMeta.color) iconEl.style.color = activeMeta.color;
    else iconEl.style.color = '';
  }
  // Re-render the menu (in case it was open or changed)
  _renderKanbanBoardMenu(boards, active);
}

// Restrict board.color to CSS hex codes or simple named colors before
// interpolating into a `style=""` attribute. esc() HTML-escapes but
// does not block CSS-context injection (`color:red;background:url(...)`
// would otherwise exfiltrate page state via an attacker-controlled URL,
// since neither this bridge nor the agent's kanban_db validates color).
function _kanbanSafeColor(c){
  if (typeof c !== 'string') return '';
  const s = c.trim();
  if (!s) return '';
  if (/^#[0-9a-fA-F]{3,8}$/.test(s)) return s;
  if (/^[a-zA-Z]{3,32}$/.test(s)) return s;
  return '';
}

function _renderKanbanBoardMenu(boards, current){
  const menu = document.getElementById('kanbanBoardSwitcherMenu');
  if (!menu) return;
  const items = boards.map(b => {
    const isCurrent = b.slug === current;
    const total = (b.total != null) ? b.total : (b.counts ? Object.values(b.counts).reduce((a,c)=>a+Number(c||0),0) : 0);
    const icon = b.icon ? esc(b.icon) : '';
    const safeColor = _kanbanSafeColor(b.color);
    const colorStyle = safeColor ? `color:${safeColor}` : '';
    return `<button type="button" class="kanban-board-switcher-item ${isCurrent ? 'is-current' : ''}" role="menuitem" data-board-slug="${esc(b.slug)}" onclick="switchKanbanBoard('${esc(b.slug)}')">
      <span class="kanban-board-switcher-item-icon" style="${colorStyle}">${icon || (isCurrent ? '✓' : '')}</span>
      <span class="kanban-board-switcher-item-name">${esc(b.name || b.slug)}</span>
      <span class="kanban-board-switcher-item-count">${esc(String(total))}</span>
    </button>`;
  }).join('');
  // Actions row — disable rename/archive when the only option is `default`
  // (the default board's display metadata is editable but the slug isn't,
  // and `default` cannot be archived).
  const renameDisabled = current === 'default';
  const archiveDisabled = current === 'default';
  const actions = `
    <div class="kanban-board-switcher-divider" role="separator"></div>
    <button type="button" class="kanban-board-switcher-action" onclick="openKanbanCreateBoard()" data-i18n="kanban_new_board">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
      <span>${esc(t('kanban_new_board') || 'New board…')}</span>
    </button>
    <button type="button" class="kanban-board-switcher-action" onclick="openKanbanRenameBoard()" ${renameDisabled ? 'disabled' : ''} data-i18n="kanban_rename_board">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
      <span>${esc(t('kanban_rename_board') || 'Rename current board…')}</span>
    </button>
    <button type="button" class="kanban-board-switcher-action danger" onclick="archiveKanbanBoard()" ${archiveDisabled ? 'disabled' : ''} data-i18n="kanban_archive_board">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
      <span>${esc(t('kanban_archive_board') || 'Archive current board…')}</span>
    </button>
  `;
  menu.innerHTML = items + actions;
}

function toggleKanbanBoardMenu(ev){
  if (ev) ev.stopPropagation();
  const menu = document.getElementById('kanbanBoardSwitcherMenu');
  const toggle = document.getElementById('kanbanBoardSwitcherToggle');
  if (!menu || !toggle) return;
  _kanbanBoardMenuOpen = !_kanbanBoardMenuOpen;
  menu.hidden = !_kanbanBoardMenuOpen;
  toggle.setAttribute('aria-expanded', String(_kanbanBoardMenuOpen));
  if (_kanbanBoardMenuOpen) {
    // Click-away close
    setTimeout(() => {
      document.addEventListener('click', _kanbanCloseBoardMenuOnOutside, {once: true, capture: true});
    }, 0);
  }
}

function _kanbanCloseBoardMenuOnOutside(ev){
  const switcher = document.getElementById('kanbanBoardSwitcher');
  if (!switcher || !switcher.contains(ev.target)) {
    _kanbanBoardMenuOpen = false;
    const menu = document.getElementById('kanbanBoardSwitcherMenu');
    const toggle = document.getElementById('kanbanBoardSwitcherToggle');
    if (menu) menu.hidden = true;
    if (toggle) toggle.setAttribute('aria-expanded', 'false');
  } else {
    // Re-arm the listener — the user clicked inside the switcher, possibly
    // the toggle button which we want to handle through its own onclick.
    setTimeout(() => {
      document.addEventListener('click', _kanbanCloseBoardMenuOnOutside, {once: true, capture: true});
    }, 0);
  }
}

async function switchKanbanBoard(slug){
  if (!slug) return;
  const newBoard = (slug === 'default') ? null : slug;
  if (newBoard === _kanbanCurrentBoard) {
    // No-op switch — just close the menu.
    _kanbanBoardMenuOpen = false;
    const menu = document.getElementById('kanbanBoardSwitcherMenu');
    if (menu) menu.hidden = true;
    return;
  }
  _kanbanCurrentBoard = newBoard;
  _kanbanSetSavedBoard(slug);
  _kanbanLatestEventId = 0;  // reset cursor — new board has its own event sequence
  _kanbanBoardMenuOpen = false;
  const menu = document.getElementById('kanbanBoardSwitcherMenu');
  if (menu) menu.hidden = true;
  // Tell the server too (sets the on-disk active-board pointer for CLI/dashboard).
  try {
    await api('/api/kanban/boards/' + encodeURIComponent(slug) + '/switch', {method: 'POST'});
  } catch(e) {
    // Local UI switch still happens — the on-disk pointer is for cross-process
    // consistency, not for our own rendering.
  }
  // Re-open the SSE stream on the new board.
  _kanbanStopPolling();
  await loadKanban(true);
  await loadKanbanBoards();
  _kanbanStartPolling();
}

// ── Create / rename / archive board modals ──────────────────────────────────

function openKanbanCreateBoard(){
  const modal = document.getElementById('kanbanBoardModal');
  if (!modal) return;
  document.getElementById('kanbanBoardModalMode').value = 'create';
  document.getElementById('kanbanBoardModalSlug').value = '';
  document.getElementById('kanbanBoardModalTitle').textContent = t('kanban_new_board') || 'New board';
  document.getElementById('kanbanBoardModalName').value = '';
  document.getElementById('kanbanBoardModalSlugInput').value = '';
  document.getElementById('kanbanBoardModalSlugInput').disabled = false;
  document.getElementById('kanbanBoardModalSlugRow').style.display = '';
  document.getElementById('kanbanBoardModalDesc').value = '';
  document.getElementById('kanbanBoardModalIcon').value = '';
  document.getElementById('kanbanBoardModalColor').value = '#7aa2ff';
  document.getElementById('kanbanBoardModalError').textContent = '';
  modal.hidden = false;
  // Auto-focus name field
  setTimeout(() => document.getElementById('kanbanBoardModalName').focus(), 50);
  // Auto-suggest slug from name as user types
  const nameEl = document.getElementById('kanbanBoardModalName');
  const slugEl = document.getElementById('kanbanBoardModalSlugInput');
  let userEditedSlug = false;
  slugEl.addEventListener('input', () => { userEditedSlug = true; }, {once: false});
  const onName = () => {
    if (!userEditedSlug) {
      slugEl.value = String(nameEl.value || '').toLowerCase().replace(/[^a-z0-9-_ ]+/g, '').replace(/\s+/g, '-').slice(0, 48);
    }
  };
  nameEl.removeEventListener('input', nameEl._kanbanOnNameInput || (() => {}));
  nameEl._kanbanOnNameInput = onName;
  nameEl.addEventListener('input', onName);
  // Close on Escape
  document.addEventListener('keydown', _kanbanBoardModalEsc);
}

function openKanbanRenameBoard(){
  const modal = document.getElementById('kanbanBoardModal');
  if (!modal) return;
  const current = _kanbanCurrentBoard || 'default';
  if (current === 'default') return;  // default's slug is immutable
  const meta = (_kanbanBoardsList || []).find(b => b.slug === current);
  if (!meta) return;
  document.getElementById('kanbanBoardModalMode').value = 'rename';
  document.getElementById('kanbanBoardModalSlug').value = current;
  document.getElementById('kanbanBoardModalTitle').textContent = t('kanban_rename_board') || 'Rename board';
  document.getElementById('kanbanBoardModalName').value = meta.name || '';
  document.getElementById('kanbanBoardModalSlugInput').value = current;
  document.getElementById('kanbanBoardModalSlugInput').disabled = true;  // slug is immutable
  // Hide the slug row — it's locked, less visual noise.
  document.getElementById('kanbanBoardModalSlugRow').style.display = 'none';
  document.getElementById('kanbanBoardModalDesc').value = meta.description || '';
  document.getElementById('kanbanBoardModalIcon').value = meta.icon || '';
  document.getElementById('kanbanBoardModalColor').value = meta.color || '#7aa2ff';
  document.getElementById('kanbanBoardModalError').textContent = '';
  modal.hidden = false;
  setTimeout(() => document.getElementById('kanbanBoardModalName').focus(), 50);
  document.addEventListener('keydown', _kanbanBoardModalEsc);
}

function _kanbanBoardModalEsc(ev){
  if (ev.key === 'Escape') closeKanbanBoardModal();
}

function closeKanbanBoardModal(){
  const modal = document.getElementById('kanbanBoardModal');
  if (modal) modal.hidden = true;
  document.removeEventListener('keydown', _kanbanBoardModalEsc);
}

async function submitKanbanBoardModal(){
  const errEl = document.getElementById('kanbanBoardModalError');
  errEl.textContent = '';
  const mode = document.getElementById('kanbanBoardModalMode').value;
  const name = (document.getElementById('kanbanBoardModalName').value || '').trim();
  const slugInput = (document.getElementById('kanbanBoardModalSlugInput').value || '').trim();
  const description = (document.getElementById('kanbanBoardModalDesc').value || '').trim();
  const icon = (document.getElementById('kanbanBoardModalIcon').value || '').trim();
  const color = (document.getElementById('kanbanBoardModalColor').value || '').trim();
  const submitBtn = document.getElementById('kanbanBoardModalSubmit');
  if (!name) {
    errEl.textContent = t('kanban_board_name_required') || 'Name is required';
    return;
  }
  if (mode === 'create') {
    if (!slugInput) {
      errEl.textContent = t('kanban_board_slug_required') || 'Slug is required';
      return;
    }
    if (submitBtn) submitBtn.disabled = true;
    try {
      const res = await api('/api/kanban/boards', {
        method: 'POST',
        body: JSON.stringify({slug: slugInput, name, description, icon, color, switch: true}),
      });
      closeKanbanBoardModal();
      // Switch to the new board and reload
      const newSlug = (res && res.board && res.board.slug) || slugInput;
      _kanbanCurrentBoard = (newSlug === 'default') ? null : newSlug;
      _kanbanSetSavedBoard(newSlug);
      _kanbanLatestEventId = 0;
      _kanbanStopPolling();
      await loadKanban(true);
      await loadKanbanBoards();
      _kanbanStartPolling();
    } catch(e) {
      errEl.textContent = (e && (e.message || e.error)) || String(e);
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  } else if (mode === 'rename') {
    const slug = document.getElementById('kanbanBoardModalSlug').value;
    if (!slug) { errEl.textContent = 'Missing slug'; return; }
    if (submitBtn) submitBtn.disabled = true;
    try {
      await api('/api/kanban/boards/' + encodeURIComponent(slug), {
        method: 'PATCH',
        body: JSON.stringify({name, description, icon, color}),
      });
      closeKanbanBoardModal();
      await loadKanbanBoards();  // refresh switcher label/icon
    } catch(e) {
      errEl.textContent = (e && (e.message || e.error)) || String(e);
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  }
}

async function archiveKanbanBoard(){
  const current = _kanbanCurrentBoard || 'default';
  if (current === 'default') return;
  const meta = (_kanbanBoardsList || []).find(b => b.slug === current);
  const label = meta && meta.name ? meta.name : current;
  const ok = await showConfirmDialog({
    title: t('kanban_archive_board') || 'Archive board',
    message: (t('kanban_archive_board_confirm') || 'Archive board "{name}"? Tasks remain on disk and the board can be restored from kanban/boards/_archived/.').replace('{name}', label),
    confirmLabel: t('kanban_archive_board') || 'Archive',
    danger: true,
    focusCancel: true,
  });
  if (!ok) return;
  // CRITICAL: stop the SSE stream BEFORE the archive call. The library's
  // kb.connect(board=<slug>) auto-creates the on-disk directory + DB on
  // first call — so any in-flight stream that polls task_events while
  // we're archiving will silently re-materialise the directory we just
  // moved to _archived/. Tearing down the stream first avoids that race.
  _kanbanStopPolling();
  try {
    await api('/api/kanban/boards/' + encodeURIComponent(current), {method: 'DELETE'});
    // Server falls back to default — match that locally.
    _kanbanCurrentBoard = null;
    _kanbanSetSavedBoard('default');
    _kanbanLatestEventId = 0;
    await loadKanban(true);
    await loadKanbanBoards();
    _kanbanStartPolling();
    showToast(t('kanban_board_archived') || 'Board archived');
  } catch(e) {
    // Restart the stream on failure so the UI doesn't go stale.
    _kanbanStartPolling();
    showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error');
  }
}


// ── Logs panel ──
function _selectedLogsFile() {
  const el = $('logsFile');
  const value = (el && el.value) || 'agent';
  return ['agent','errors','gateway'].includes(value) ? value : 'agent';
}

function _selectedLogsTail() {
  const el = $('logsTail');
  const value = Number((el && el.value) || 200);
  return [100,200,500,1000].includes(value) ? value : 200;
}

function _logLineSeverityClass(line) {
  const text = String(line || '').toUpperCase();
  if (/\b(WARNING|WARN)\b/.test(text)) return 'log-line-warning';
  if (/\b(DEBUG)\b/.test(text)) return 'log-line-debug';
  if (/\b(INFO)\b/.test(text)) return 'log-line-info';
  if (/\b(ERROR|CRITICAL|TRACEBACK)\b/.test(text)) return 'log-line-error';
  return '';
}

function _syncLogsWrap() {
  const out = $('logsOutput');
  const wrap = $('logsWrap');
  if (out && wrap) out.classList.toggle('wrap', !!wrap.checked);
}

async function loadLogs(animate) {
  const box = $('logsOutput');
  const status = $('logsStatus');
  const refreshBtn = $('logsRefreshBtn');
  if (!box) return;
  if (animate && refreshBtn) {
    refreshBtn.style.opacity = '0.5';
    refreshBtn.disabled = true;
  }
  const file = _selectedLogsFile();
  const tail = _selectedLogsTail();
  try {
    if (status) status.textContent = t('logs_loading');
    const data = await api('/api/logs?file=' + encodeURIComponent(file) + '&tail=' + encodeURIComponent(tail));
    _renderLogs(data);
  } catch(e) {
    _lastLogsLines = [];
    box.innerHTML = `<div class="logs-empty">${esc(t('error_prefix') + e.message)}</div>`;
    if (status) status.textContent = t('logs_load_failed');
  } finally {
    if (animate && refreshBtn) {
      refreshBtn.style.opacity = '';
      refreshBtn.disabled = false;
    }
    _syncLogsAutoRefresh();
  }
}

function _renderLogs(data) {
  const box = $('logsOutput');
  const status = $('logsStatus');
  if (!box) return;
  const lines = Array.isArray(data && data.lines) ? data.lines : [];
  _lastLogsLines = lines.slice();
  const hint = data && data.hint ? `<div class="logs-hint">${esc(data.hint)}</div>` : '';
  const truncated = data && data.truncated ? `<div class="logs-hint warn">${esc(t('logs_truncated_hint'))}</div>` : '';
  if (!lines.length) {
    box.innerHTML = `${hint}${truncated}<div class="logs-empty">${esc(t('logs_empty'))}</div>`;
  } else {
    box.innerHTML = `${hint}${truncated}` + lines.map(line => {
      const cls = _logLineSeverityClass(line);
      return `<div class="log-line ${cls}">${esc(line)}</div>`;
    }).join('');
  }
  _syncLogsWrap();
  if (status) {
    const bytes = data && Number(data.total_bytes || 0);
    const when = data && data.mtime ? new Date(data.mtime * 1000).toLocaleString() : t('logs_no_mtime');
    status.textContent = `${lines.length} / ${data.tail || _selectedLogsTail()} lines · ${bytes.toLocaleString()} bytes · ${when}`;
  }
}

function _startLogsAutoRefresh() {
  if (_logsAutoRefreshTimer) return;
  _logsAutoRefreshTimer = setInterval(() => {
    if (_currentPanel !== 'logs') { _stopLogsAutoRefresh(); return; }
    const toggle = $('logsAutoRefresh');
    if (toggle && !toggle.checked) return;
    loadLogs(false);
  }, 5000);
}

function _stopLogsAutoRefresh() {
  if (_logsAutoRefreshTimer) {
    clearInterval(_logsAutoRefreshTimer);
    _logsAutoRefreshTimer = null;
  }
}

function _syncLogsAutoRefresh() {
  const toggle = $('logsAutoRefresh');
  if (_currentPanel === 'logs' && (!toggle || toggle.checked)) _startLogsAutoRefresh();
  else _stopLogsAutoRefresh();
}

async function copyLogsAll() {
  const text = _lastLogsLines.join('\n');
  try {
    await navigator.clipboard.writeText(text);
    showToast(t('logs_copied'));
  } catch(e) {
    showToast(t('copy_failed'), 'error');
  }
}

// ── Insights panel ──
async function loadInsights(animate) {
  const box = $('insightsContent');
  const refreshBtn = $('insightsRefreshBtn');
  if (!box) return;
  if (animate && refreshBtn) {
    refreshBtn.style.opacity = '0.5';
    refreshBtn.disabled = true;
  }
  const period = ($('insightsPeriod') || {}).value || '30';
  try {
    const [data, wikiStatus] = await Promise.all([
      api(`/api/insights?days=${period}`),
      api('/api/wiki/status').catch(err => ({status:'error', error: err.message || String(err)})),
    ]);
    _renderInsights(data, box, wikiStatus);
    if (typeof _syncSystemHealthMonitorVisibility === 'function') _syncSystemHealthMonitorVisibility();
    if (typeof pollSystemHealth === 'function') void pollSystemHealth();
  } catch(e) {
    box.innerHTML = `<div style="color:var(--accent);font-size:12px">${esc(t('error_prefix') + e.message)}</div>`;
  } finally {
    if (animate && refreshBtn) {
      refreshBtn.style.opacity = '';
      refreshBtn.disabled = false;
    }
  }
}

function _formatLlmWikiTimestamp(value) {
  if (!value) return 'Never';
  try { return new Date(value).toLocaleString(); }
  catch (_) { return String(value); }
}

function _renderSystemHealthPanel() {
  return `
    <section class="insights-card system-health-panel loading" id="systemHealthPanel" aria-label="Host resource health" aria-live="polite">
      <div class="system-health-head">
        <div>
          <div class="insights-card-title">System health</div>
          <div class="system-health-sub">Current VPS resource usage</div>
        </div>
        <span class="system-health-status" id="systemHealthStatus"><span class="system-health-dot" aria-hidden="true"></span>Loading…</span>
      </div>
      <div class="system-health-metrics">
        <div class="system-health-metric" data-system-health-metric="cpu">
          <div class="system-health-label"><span>CPU</span><span class="system-health-value" data-system-health-value>—</span></div>
          <div class="system-health-bar" role="progressbar" aria-label="CPU usage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="system-health-bar-fill"></div></div>
        </div>
        <div class="system-health-metric" data-system-health-metric="memory">
          <div class="system-health-label"><span>RAM</span><span class="system-health-value" data-system-health-value>—</span></div>
          <div class="system-health-bar" role="progressbar" aria-label="RAM usage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="system-health-bar-fill"></div></div>
        </div>
        <div class="system-health-metric" data-system-health-metric="disk">
          <div class="system-health-label"><span>Disk</span><span class="system-health-value" data-system-health-value>—</span></div>
          <div class="system-health-bar" role="progressbar" aria-label="Disk usage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="system-health-bar-fill"></div></div>
        </div>
      </div>
      <div class="system-health-foot">Live snapshot only; historical resource charts can build on this surface later.</div>
    </section>`;
}

function _renderLlmWikiStatus(d) {
  const status = d || {status:'error'};
  const isReady = status.available && status.status === 'ready';
  const isEmpty = status.available && status.status === 'empty';
  const isError = status.status === 'error';
  const badgeClass = isReady ? 'ok' : isError ? 'err' : isEmpty ? 'warn' : 'muted';
  const badgeText = isReady ? 'Available' : isError ? 'Error' : isEmpty ? 'Empty' : 'Unavailable';
  const rawDocsUrl = status.docs_url || 'https://hermes-agent.nousresearch.com/docs/user-guide/skills/bundled/research/research-llm-wiki';
  // Guard against unsafe URL schemes (e.g. js: / data:) if docs_url ever
  // becomes config-driven. esc() HTML-escapes but doesn't validate URL scheme.
  const docsUrl = /^https?:\/\//i.test(rawDocsUrl) ? rawDocsUrl : '#';
  const toggleNote = status.toggle_available
    ? 'Toggle available from configured Hermes Agent setting.'
    : (status.toggle_reason || 'No stable LLM Wiki on/off config flag was detected, so this panel is read-only.');
  const statusNote = isReady
    ? 'LLM Wiki is configured and page metadata is visible without exposing wiki content.'
    : isEmpty
      ? 'LLM Wiki exists but has no entity, concept, comparison, or query pages yet.'
      : isError
        ? `Unable to inspect LLM Wiki status${status.error ? ': ' + status.error : ''}.`
        : 'No LLM Wiki directory was found. Set WIKI_PATH or skills.config.wiki.path to enable status visibility.';
  return `
    <div class="insights-card wiki-status-card" id="llmWikiStatusCard">
      <div class="wiki-status-head">
        <div>
          <div class="insights-card-title">LLM Wiki</div>
          <div class="wiki-status-sub">Knowledge-base observability</div>
        </div>
        <span class="wiki-status-badge ${badgeClass}">${esc(badgeText)}</span>
      </div>
      <div class="wiki-status-note">${esc(statusNote)}</div>
      <div class="wiki-status-grid">
        <div><span>Enabled</span><strong>${status.enabled ? 'Yes' : 'No'}</strong></div>
        <div><span>Entries</span><strong>${Number(status.entry_count || 0).toLocaleString()}</strong></div>
        <div><span>Pages</span><strong>${Number(status.page_count || 0).toLocaleString()}</strong></div>
        <div><span>raw/ files</span><strong>${Number(status.raw_source_count || 0).toLocaleString()}</strong></div>
        <div><span>Last updated</span><strong>${esc(_formatLlmWikiTimestamp(status.last_updated))}</strong></div>
        <div><span>Last writer</span><strong>${esc(status.last_writer || 'Not available')}</strong></div>
      </div>
      <div class="wiki-status-footer">
        <span>${esc(toggleNote)}</span>
        <a href="${esc(docsUrl)}" target="_blank" rel="noopener noreferrer">Docs</a>
      </div>
    </div>`;
}

function _renderInsights(d, box, wikiStatus) {
  const fmtNum = n => Number(n || 0).toLocaleString();
  const fmtCost = c => {
    const value = Number(c || 0);
    return value > 0 ? '$' + value.toFixed(value < 1 ? 4 : 2) : t('insights_no_cost');
  };
  const fmtTokens = n => {
    const value = Number(n || 0);
    return value >= 1e6 ? (value/1e6).toFixed(1) + 'M' : value >= 1e3 ? (value/1e3).toFixed(1) + 'K' : fmtNum(value);
  };

  // Overview cards
  const overviewCards = [
    { label: t('insights_sessions'), value: fmtNum(d.total_sessions), icon: li('message-square', 18) },
    { label: t('insights_messages'), value: fmtNum(d.total_messages), icon: li('hash', 18) },
    { label: t('insights_tokens'), value: fmtTokens(d.total_tokens), icon: li('cpu', 18) },
    { label: t('insights_cost'), value: fmtCost(d.total_cost), icon: li('dollar-sign', 18) },
  ];

  // Daily token trend
  const dailyTokens = Array.isArray(d.daily_tokens) ? d.daily_tokens : [];
  let dailyHtml = '';
  if (dailyTokens.length) {
    const maxDailyTokens = Math.max(...dailyTokens.map(r => Number(r.input_tokens || 0) + Number(r.output_tokens || 0)), 1);
    const labelEvery = Math.max(Math.ceil(dailyTokens.length / 7), 1);
    dailyHtml = `<div class="insights-card"><div class="insights-card-title">${esc(t('insights_daily_tokens'))}</div><div class="insights-daily-token-chart">` +
      dailyTokens.map((r, idx) => {
        const input = Number(r.input_tokens || 0);
        const output = Number(r.output_tokens || 0);
        const inputPct = Math.max((input / maxDailyTokens) * 100, input ? 2 : 0).toFixed(1);
        const outputPct = Math.max((output / maxDailyTokens) * 100, output ? 2 : 0).toFixed(1);
        const showLabel = idx === 0 || idx === dailyTokens.length - 1 || idx % labelEvery === 0;
        const title = `${r.date} · ${fmtTokens(input)} ${t('insights_input_tokens')} · ${fmtTokens(output)} ${t('insights_output_tokens')} · ${fmtCost(r.cost)} · ${fmtNum(r.sessions)} ${t('insights_sessions')}`;
        return `<div class="insights-daily-bar" title="${esc(title)}"><div class="insights-daily-stack" aria-label="${esc(title)}"><div class="insights-daily-bar-output" style="height:${outputPct}%"></div><div class="insights-daily-bar-input" style="height:${inputPct}%"></div></div><span>${showLabel ? esc(String(r.date).slice(5)) : ''}</span></div>`;
      }).join('') +
      `</div><div class="insights-daily-legend"><span><i class="insights-daily-legend-input"></i>${esc(t('insights_input_tokens'))}</span><span><i class="insights-daily-legend-output"></i>${esc(t('insights_output_tokens'))}</span></div></div>`;
  } else {
    dailyHtml = `<div class="insights-card"><div class="insights-card-title">${esc(t('insights_daily_tokens'))}</div><div class="insights-empty">${esc(t('insights_no_usage_data'))}</div></div>`;
  }

  // Models table
  let modelsHtml = '';
  if (d.models && d.models.length) {
    modelsHtml = `<div class="insights-card"><div class="insights-card-title">${esc(t('insights_models'))}</div><div class="insights-table insights-model-table"><div class="insights-table-head"><span>${esc(t('insights_model_name'))}</span><span>${esc(t('insights_model_sessions'))}</span><span>${esc(t('insights_model_tokens'))}</span><span>${esc(t('insights_model_cost'))}</span><span>${esc(t('insights_model_share'))}</span></div>` +
      d.models.map(m => {
        const share = Number(m.cost_share || m.token_share || m.session_share || 0);
        const title = `${m.model} · ${fmtTokens(m.input_tokens)} ${t('insights_input_tokens')} · ${fmtTokens(m.output_tokens)} ${t('insights_output_tokens')}`;
        return `<div class="insights-table-row"><span class="insights-model-name" title="${esc(m.model)}">${esc(m.model)}</span><span>${fmtNum(m.sessions)}</span><span class="insights-model-tokens" title="${esc(title)}">${fmtTokens(m.total_tokens || 0)}</span><span class="insights-model-cost">${fmtCost(m.cost)}</span><span>${share}%</span></div>`;
      }).join('') +
      `</div></div>`;
  } else {
    modelsHtml = `<div class="insights-card"><div class="insights-card-title">${esc(t('insights_models'))}</div><div class="insights-empty">${esc(t('insights_no_usage_data'))}</div></div>`;
  }

  // Activity by day of week
  let dowHtml = '';
  if (d.activity_by_day) {
    const maxDow = Math.max(...d.activity_by_day.map(x => x.sessions), 1);
    dowHtml = `<div class="insights-card"><div class="insights-card-title">${esc(t('insights_activity_by_day'))}</div><div class="insights-bars">` +
      d.activity_by_day.map(r => {
        const pct = (r.sessions / maxDow * 100).toFixed(0);
        return `<div class="insights-bar-row"><span class="insights-bar-label">${r.day}</span><div class="insights-bar-track"><div class="insights-bar-fill" style="width:${pct}%"></div></div><span class="insights-bar-value">${r.sessions}</span></div>`;
      }).join('') +
      `</div></div>`;
  }

  // Activity by hour
  let hodHtml = '';
  if (d.activity_by_hour) {
    const maxHod = Math.max(...d.activity_by_hour.map(x => x.sessions), 1);
    const peakHour = d.activity_by_hour.reduce((a, b) => b.sessions > a.sessions ? b : a, {hour:0,sessions:0});
    hodHtml = `<div class="insights-card"><div class="insights-card-title">${esc(t('insights_activity_by_hour'))} <span style="font-weight:400;font-size:11px;color:var(--muted)">${esc(t('insights_peak_hour').replace('{hour}', peakHour.hour + ':00'))}</span></div><div class="insights-bars">` +
      d.activity_by_hour.map(r => {
        const pct = (r.sessions / maxHod * 100).toFixed(0);
        const isPeak = r.hour === peakHour.hour && peakHour.sessions > 0;
        return `<div class="insights-bar-row"><span class="insights-bar-label">${String(r.hour).padStart(2,'0')}</span><div class="insights-bar-track"><div class="insights-bar-fill${isPeak ? ' insights-bar-peak' : ''}" style="width:${pct}%"></div></div><span class="insights-bar-value">${r.sessions}</span></div>`;
      }).join('') +
      `</div></div>`;
  }

  // Token breakdown
  const tokenCards = `
    <div class="insights-card">
      <div class="insights-card-title">${esc(t('insights_token_breakdown'))}</div>
      <div class="insights-token-row">
        <span class="insights-token-label">${esc(t('insights_input_tokens'))}</span>
        <span class="insights-token-value">${fmtTokens(d.total_input_tokens)}</span>
      </div>
      <div class="insights-token-row">
        <span class="insights-token-label">${esc(t('insights_output_tokens'))}</span>
        <span class="insights-token-value">${fmtTokens(d.total_output_tokens)}</span>
      </div>
      <div class="insights-token-row insights-token-total">
        <span class="insights-token-label">${esc(t('insights_total'))}</span>
        <span class="insights-token-value">${fmtTokens(d.total_tokens)}</span>
      </div>
    </div>`;

  box.innerHTML = `
    ${_renderSystemHealthPanel()}
    ${_renderLlmWikiStatus(wikiStatus)}
    <div class="insights-grid">
      ${overviewCards.map(c => `<div class="insights-stat"><div class="insights-stat-icon">${c.icon}</div><div class="insights-stat-info"><div class="insights-stat-value">${c.value}</div><div class="insights-stat-label">${esc(c.label)}</div></div></div>`).join('')}
    </div>
    ${dailyHtml}
    <div class="insights-row">
      ${tokenCards}
      ${modelsHtml}
    </div>
    ${dowHtml}
    ${hodHtml}
    <div style="text-align:center;color:var(--muted);font-size:10px;margin-top:12px;opacity:.6">${esc(t('insights_footer').replace('{days}', d.period_days))}</div>
  `;
}

async function clearConversation() {
  if(!S.session) return;
  const _clrMsg=await showConfirmDialog({title:t('clear_conversation_title'),message:t('clear_conversation_message'),confirmLabel:t('clear'),danger:true,focusCancel:true});
  if(!_clrMsg) return;
  try {
    const data = await api('/api/session/clear', {method:'POST',
      body: JSON.stringify({session_id: S.session.session_id})});
    S.session = data.session;
    S.messages = [];
    S.toolCalls = [];
    syncTopbar();
    renderMessages();
    showToast(t('conversation_cleared'));
  } catch(e) { setStatus(t('clear_failed') + e.message); }
}

// ── Skills panel ──
async function loadSkills() {
  if (_skillsData) { renderSkills(_skillsData); return; }
  const box = $('skillsList');
  try {
    const data = await api('/api/skills');
    _skillsData = data.skills || [];
    // Prune collapsed state to only keep categories present in fresh data,
    // avoiding stale keys when categories are renamed or removed server-side.
    const liveCats = new Set(_skillsData.map(s => s.category || '(general)'));
    for (const c of _collapsedCats) { if (!liveCats.has(c)) _collapsedCats.delete(c); }
    renderSkills(_skillsData);
  } catch(e) { box.innerHTML = `<div style="padding:12px;color:var(--accent);font-size:12px">Error: ${esc(e.message)}</div>`; }
}

let _collapsedCats = new Set(); // persisted collapsed state across re-renders

function _toggleCatCollapse(cat) {
  if (_collapsedCats.has(cat)) _collapsedCats.delete(cat);
  else _collapsedCats.add(cat);
  // Toggle DOM without full re-render
  document.querySelectorAll('.skills-category').forEach(sec => {
    const header = sec.querySelector('.skills-cat-header');
    if (header && header.dataset.cat === cat) {
      const collapsed = _collapsedCats.has(cat);
      sec.classList.toggle('collapsed', collapsed);
      header.querySelector('.cat-chevron').style.transform = collapsed ? '' : 'rotate(90deg)';
      sec.querySelectorAll('.skill-item').forEach(el => el.style.display = collapsed ? 'none' : '');
    }
  });
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
  if (!filtered.length) { box.innerHTML = `<div style="padding:12px;color:var(--muted);font-size:12px">${esc(t('skills_no_match'))}</div>`; return; }
  for (const [cat, items] of Object.entries(cats).sort()) {
    const collapsed = _collapsedCats.has(cat);
    const sec = document.createElement('div');
    sec.className = 'skills-category' + (collapsed ? ' collapsed' : '');
    const hdr = document.createElement('div');
    hdr.className = 'skills-cat-header';
    hdr.dataset.cat = cat;
    hdr.innerHTML = `<span class="cat-chevron" style="display:inline-flex;transition:transform .15s;${collapsed ? '' : 'transform:rotate(90deg)'}">${li('chevron-right',12)}</span> ${esc(cat)} <span style="opacity:.5">(${items.length})</span>`;
    hdr.onclick = () => _toggleCatCollapse(cat);
    sec.appendChild(hdr);
    for (const skill of items.sort((a,b) => a.name.localeCompare(b.name))) {
      const el = document.createElement('div');
      el.className = 'skill-item';
      el.style.display = collapsed ? 'none' : '';
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

// Currently selected skill detail — kept across panel switches so re-entering
// the Skills view shows the last-viewed skill.
let _currentSkillDetail = null; // { name, category, content }
let _skillMode = 'empty'; // 'empty' | 'read' | 'create' | 'edit'
let _skillPreFormDetail = null; // snapshot of previously-viewed skill when entering a form
let _editingSkillName = null;

function _stripYamlFrontmatter(content) {
  if (!content) return { frontmatter: null, body: '' };
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(content);
  if (!m) return { frontmatter: null, body: content };
  return { frontmatter: m[1], body: content.slice(m[0].length) };
}

function _renderSkillDetail(name, content, linkedFiles) {
  const title = $('skillDetailTitle');
  const body = $('skillDetailBody');
  const empty = $('skillDetailEmpty');
  const editBtn = $('btnEditSkillDetail');
  const delBtn = $('btnDeleteSkillDetail');
  if (title) title.textContent = name;
  const { frontmatter, body: markdownBody } = _stripYamlFrontmatter(content);
  let html = '';
  if (frontmatter) {
    html += `<details class="skill-frontmatter"><summary>${esc(t('skill_metadata'))}</summary><pre><code>${esc(frontmatter)}</code></pre></details>`;
  }
  html += renderMd(markdownBody || '(no content)');
  const lf = linkedFiles || {};
  const categories = Object.entries(lf).filter(([,files]) => files && files.length > 0);
  if (categories.length) {
    html += `<div class="skill-linked-files"><div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">${esc(t('linked_files'))}</div>`;
    for (const [cat, files] of categories) {
      html += `<div class="skill-linked-section"><h4>${esc(cat)}</h4>`;
      for (const f of files) {
        html += `<a class="skill-linked-file" href="#" data-skill-name="${esc(name)}" data-skill-file="${esc(f)}">${esc(f)}</a>`;
      }
      html += '</div>';
    }
    html += '</div>';
  }
  body.innerHTML = `<div class="main-view-content skill-detail-content">${html}</div>`;
  body.querySelectorAll('.skill-linked-file').forEach(a => {
    a.addEventListener('click', e => { e.preventDefault(); openSkillFile(a.dataset.skillName, a.dataset.skillFile); });
  });
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _skillMode = 'read';
  _setSkillHeaderButtons('read');
}

function _setSkillHeaderButtons(mode) {
  const editBtn = $('btnEditSkillDetail');
  const delBtn = $('btnDeleteSkillDetail');
  const cancelBtn = $('btnCancelSkillDetail');
  const saveBtn = $('btnSaveSkillDetail');
  const show = b => b && (b.style.display = '');
  const hide = b => b && (b.style.display = 'none');
  if (mode === 'read') { show(editBtn); show(delBtn); hide(cancelBtn); hide(saveBtn); }
  else if (mode === 'create' || mode === 'edit') { hide(editBtn); hide(delBtn); show(cancelBtn); show(saveBtn); }
  else { hide(editBtn); hide(delBtn); hide(cancelBtn); hide(saveBtn); }
}

async function openSkill(name, el) {
  // Highlight active skill in the sidebar list
  document.querySelectorAll('.skill-item').forEach(e => e.classList.remove('active'));
  if (el) el.classList.add('active');
  _skillPreFormDetail = null;
  _editingSkillName = null;
  try {
    const data = await api(`/api/skills/content?name=${encodeURIComponent(name)}`);
    _currentSkillDetail = { name, content: data.content || '', linked_files: data.linked_files || {} };
    _renderSkillDetail(name, data.content || '', data.linked_files || {});
  } catch(e) { setStatus(t('skill_load_failed') + e.message); }
}

async function openSkillFile(skillName, filePath) {
  try {
    const data = await api(`/api/skills/content?name=${encodeURIComponent(skillName)}&file=${encodeURIComponent(filePath)}`);
    const body = $('skillDetailBody');
    if (!body) return;
    const ext = (filePath.split('.').pop() || '').toLowerCase();
    const isMd = ['md','markdown'].includes(ext);
    const backLabel = t('skills_back_to').replace('{0}', skillName);
    const header = `<div class="skill-file-breadcrumb"><a href="#" class="skill-file-back" data-skill-name="${esc(skillName)}">&larr; ${esc(backLabel)}</a><span class="skill-file-path">${esc(filePath)}</span></div>`;
    let content;
    if (isMd) {
      content = `<div class="main-view-content">${renderMd(data.content || '')}</div>`;
    } else {
      const escaped = esc(data.content || '');
      content = `<pre class="skill-file-code"><code>${escaped}</code></pre>`;
    }
    body.innerHTML = header + content;
    body.style.display = '';
    const empty = $('skillDetailEmpty');
    if (empty) empty.style.display = 'none';
    body.querySelectorAll('.skill-file-back').forEach(a => {
      a.addEventListener('click', e => {
        e.preventDefault();
        if (_currentSkillDetail && _currentSkillDetail.name === a.dataset.skillName) {
          _renderSkillDetail(_currentSkillDetail.name, _currentSkillDetail.content, _currentSkillDetail.linked_files);
        } else {
          openSkill(a.dataset.skillName, null);
        }
      });
    });
    if (!isMd) requestAnimationFrame(() => { if (typeof highlightCode === 'function') highlightCode(); });
  } catch(e) { setStatus(t('skill_file_load_failed') + e.message); }
}

function editCurrentSkill() {
  if (!_currentSkillDetail) return;
  const s = _currentSkillDetail;
  let category = '';
  if (_skillsData) {
    const match = _skillsData.find(x => x.name === s.name);
    if (match) category = match.category || '';
  }
  _skillPreFormDetail = { name: s.name, content: s.content, linked_files: s.linked_files };
  _editingSkillName = s.name;
  _skillMode = 'edit';
  _renderSkillForm({ name: s.name, category, content: s.content || '', isEdit: true });
}

function openSkillCreate() {
  if (typeof switchPanel === 'function' && _currentPanel !== 'skills') switchPanel('skills');
  _skillPreFormDetail = _currentSkillDetail ? { ..._currentSkillDetail } : null;
  _editingSkillName = null;
  _skillMode = 'create';
  _renderSkillForm({ name: '', category: '', content: '', isEdit: false });
}

function _renderSkillForm({ name, category, content, isEdit }) {
  const title = $('skillDetailTitle');
  const body = $('skillDetailBody');
  const empty = $('skillDetailEmpty');
  if (!body || !title) return;
  title.textContent = isEdit ? t('skills_edit') + ' · ' + name : t('new_skill');
  const nameDisabled = isEdit ? 'disabled' : '';
  const nameHint = isEdit ? `<div class="detail-form-hint">${esc(t('skill_rename_not_supported') || 'Renaming a skill is not supported. Create a new skill and delete the old one to rename.')}</div>` : '';
  body.innerHTML = `
    <div class="main-view-content">
      <form class="detail-form" onsubmit="event.preventDefault(); saveSkillForm();">
        <div class="detail-form-row">
          <label for="skillFormName">${esc(t('skill_name') || 'Name')}</label>
          <input type="text" id="skillFormName" value="${esc(name || '')}" placeholder="my-skill" autocomplete="off" ${nameDisabled} required>
          ${nameHint}
        </div>
        <div class="detail-form-row">
          <label for="skillFormCategory">${esc(t('skill_category') || 'Category')}</label>
          <input type="text" id="skillFormCategory" value="${esc(category || '')}" placeholder="${esc(t('skill_category_placeholder') || 'Optional, e.g. devops')}" autocomplete="off">
        </div>
        <div class="detail-form-row">
          <label for="skillFormContent">${esc(t('skill_content') || 'SKILL.md content')}</label>
          <textarea id="skillFormContent" rows="18" placeholder="${esc(t('skill_content_placeholder') || 'YAML frontmatter + markdown body')}">${esc(content || '')}</textarea>
        </div>
        <div id="skillFormError" class="detail-form-error" style="display:none"></div>
      </form>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _setSkillHeaderButtons(isEdit ? 'edit' : 'create');
  const focusEl = isEdit ? $('skillFormCategory') : $('skillFormName');
  if (focusEl) focusEl.focus();
}

function cancelSkillForm() {
  _editingSkillName = null;
  if (_skillPreFormDetail) {
    const snap = _skillPreFormDetail;
    _skillPreFormDetail = null;
    _currentSkillDetail = snap;
    _renderSkillDetail(snap.name, snap.content || '', snap.linked_files || {});
    return;
  }
  // Revert to empty state
  _skillPreFormDetail = null;
  _currentSkillDetail = null;
  _skillMode = 'empty';
  const body = $('skillDetailBody');
  const empty = $('skillDetailEmpty');
  const title = $('skillDetailTitle');
  if (body) { body.innerHTML = ''; body.style.display = 'none'; }
  if (empty) empty.style.display = '';
  if (title) title.textContent = '';
  _setSkillHeaderButtons('empty');
}

async function saveSkillForm() {
  const nameInput = $('skillFormName');
  const catInput = $('skillFormCategory');
  const contentInput = $('skillFormContent');
  const errEl = $('skillFormError');
  if (!nameInput || !contentInput || !errEl) return;
  const name = (nameInput.value || '').trim().toLowerCase().replace(/\s+/g, '-');
  const category = (catInput ? (catInput.value || '').trim() : '');
  const content = contentInput.value;
  errEl.style.display = 'none';
  if (!name) { errEl.textContent = t('skill_name_required'); errEl.style.display = ''; return; }
  if (!content.trim()) { errEl.textContent = t('content_required'); errEl.style.display = ''; return; }
  try {
    await api('/api/skills/save', {method:'POST', body: JSON.stringify({name, category: category||undefined, content})});
    showToast(_editingSkillName ? t('skill_updated') : t('skill_created'));
    _skillsData = null;
    _cronSkillsCache = null;
    _editingSkillName = null;
    _skillPreFormDetail = null;
    await loadSkills();
    // Reload the saved skill in read mode with fresh content
    const row = document.querySelector(`.skill-item .skill-name`);
    const match = document.querySelectorAll('.skill-item');
    let targetEl = null;
    match.forEach(el => {
      const nm = el.querySelector('.skill-name');
      if (nm && nm.textContent === name) targetEl = el;
    });
    await openSkill(name, targetEl);
  } catch(e) { errEl.textContent = t('error_prefix') + e.message; errEl.style.display = ''; }
}

// Back-compat aliases (delete flow + any old callers)
const submitSkillSave = saveSkillForm;
function toggleSkillForm(){ openSkillCreate(); }

async function deleteCurrentSkill() {
  if (!_currentSkillDetail) return;
  const name = _currentSkillDetail.name;
  const message = t('skill_delete_confirm')
    ? t('skill_delete_confirm').replace('{0}', name)
    : `Delete skill "${name}"?`;
  const ok = await showConfirmDialog({
    title: t('delete_title') || 'Delete',
    message,
    confirmLabel: t('delete_title') || 'Delete',
    danger: true,
    focusCancel: true,
  });
  if (!ok) return;
  try {
    await api('/api/skills/delete', { method:'POST', body: JSON.stringify({ name }) });
    _currentSkillDetail = null;
    _skillPreFormDetail = null;
    _skillsData = null;
    _cronSkillsCache = null;
    _skillMode = 'empty';
    const body = $('skillDetailBody');
    const empty = $('skillDetailEmpty');
    const title = $('skillDetailTitle');
    if (body) { body.innerHTML = ''; body.style.display = 'none'; }
    if (empty) empty.style.display = '';
    if (title) title.textContent = '';
    _setSkillHeaderButtons('empty');
    await loadSkills();
    showToast(t('skill_deleted') || 'Skill deleted');
  } catch(e) { setStatus(t('error_prefix') + e.message); }
}

// ── Memory (main view) ──
let _memoryData = null;
let _currentMemorySection = null; // 'memory' | 'user'
let _memoryMode = 'empty'; // 'empty' | 'read' | 'edit'

const MEMORY_SECTIONS = [
  { key: 'memory', labelKey: 'my_notes', emptyKey: 'no_notes_yet', iconKey: 'brain' },
  { key: 'user',   labelKey: 'user_profile', emptyKey: 'no_profile_yet', iconKey: 'user' },
];

function _memorySectionMeta(key) {
  return MEMORY_SECTIONS.find(s => s.key === key) || MEMORY_SECTIONS[0];
}

function _memorySectionContent(key) {
  if (!_memoryData) return '';
  return key === 'user' ? (_memoryData.user || '') : (_memoryData.memory || '');
}

function _memorySectionMtime(key) {
  if (!_memoryData) return 0;
  return key === 'user' ? (_memoryData.user_mtime || 0) : (_memoryData.memory_mtime || 0);
}

function _setMemoryHeaderButtons(mode) {
  const show = b => b && (b.style.display = '');
  const hide = b => b && (b.style.display = 'none');
  const editBtn = $('btnEditMemoryDetail');
  const cancelBtn = $('btnCancelMemoryDetail');
  const saveBtn = $('btnSaveMemoryDetail');
  if (mode === 'read') { show(editBtn); hide(cancelBtn); hide(saveBtn); }
  else if (mode === 'edit') { hide(editBtn); show(cancelBtn); show(saveBtn); }
  else { hide(editBtn); hide(cancelBtn); hide(saveBtn); }
}

function _renderMemoryDetail(section) {
  const meta = _memorySectionMeta(section);
  const title = $('memoryDetailTitle');
  const body = $('memoryDetailBody');
  const empty = $('memoryDetailEmpty');
  if (!title || !body) return;
  title.textContent = t(meta.labelKey);
  const content = _memorySectionContent(section);
  const mtime = _memorySectionMtime(section);
  const mtimeStr = mtime ? new Date(mtime * 1000).toLocaleString() : '';
  const mtimeHtml = mtimeStr ? `<div class="memory-detail-mtime">${esc(mtimeStr)}</div>` : '';
  const inner = content
    ? `<div class="memory-content preview-md">${renderMd(content)}</div>`
    : `<div class="memory-empty">${esc(t(meta.emptyKey))}</div>`;
  body.innerHTML = `<div class="main-view-content">${mtimeHtml}${inner}</div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _memoryMode = 'read';
  _setMemoryHeaderButtons('read');
}

function _renderMemoryEdit(section) {
  const meta = _memorySectionMeta(section);
  const title = $('memoryDetailTitle');
  const body = $('memoryDetailBody');
  const empty = $('memoryDetailEmpty');
  if (!title || !body) return;
  title.textContent = t(meta.labelKey);
  const content = _memorySectionContent(section);
  body.innerHTML = `
    <div class="main-view-content">
      <form class="detail-form" onsubmit="event.preventDefault(); submitMemorySave();">
        <div class="detail-form-row">
          <label for="memEditContent">${esc(t('memory_notes_label'))}</label>
          <textarea id="memEditContent" rows="20" spellcheck="false">${esc(content)}</textarea>
        </div>
        <div id="memEditError" class="detail-form-error" style="display:none"></div>
      </form>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _memoryMode = 'edit';
  _setMemoryHeaderButtons('edit');
  const ta = $('memEditContent');
  if (ta) ta.focus();
}

function openMemorySection(section, el) {
  _currentMemorySection = section;
  document.querySelectorAll('#memoryPanel .side-menu-item').forEach(e => e.classList.remove('active'));
  if (el) el.classList.add('active');
  _renderMemoryDetail(section);
}

function editCurrentMemory() {
  if (!_currentMemorySection) return;
  _renderMemoryEdit(_currentMemorySection);
}

function cancelMemoryEdit() {
  if (!_currentMemorySection) return;
  _renderMemoryDetail(_currentMemorySection);
}

// Legacy alias (kept for any stale references)
function toggleMemoryEdit() { editCurrentMemory(); }
function closeMemoryEdit() { cancelMemoryEdit(); }

async function submitMemorySave() {
  if (!_currentMemorySection) return;
  const ta = $('memEditContent');
  const errEl = $('memEditError');
  if (!ta) return;
  if (errEl) errEl.style.display = 'none';
  try {
    await api('/api/memory/write', {method:'POST', body: JSON.stringify({section: _currentMemorySection, content: ta.value})});
    showToast(t('memory_saved'));
    await loadMemory(true);
    _renderMemoryDetail(_currentMemorySection);
  } catch(e) {
    if (errEl) { errEl.textContent = t('error_prefix') + e.message; errEl.style.display = ''; }
  }
}

// ── Workspace management ──
let _workspaceList = [];  // cached from /api/workspaces
let _wsSuggestTimer = null;
let _wsSuggestReq = 0;
let _wsSuggestIndex = -1;

function closeWorkspacePathSuggestions(){
  const box=$('workspaceFormPathSuggestions');
  if(box){
    box.innerHTML='';
    box.style.display='none';
  }
  _wsSuggestIndex=-1;
}

function _applyWorkspaceSuggestion(path){
  const input=$('workspaceFormPath');
  const next=(path||'').endsWith('/')?(path||''):`${path||''}/`;
  if(input){
    input.value=next;
    input.focus();
    input.setSelectionRange(next.length, next.length);
  }
  scheduleWorkspacePathSuggestions();
}

function _highlightWorkspaceSuggestion(idx){
  const box=$('workspaceFormPathSuggestions');
  if(!box)return;
  const items=[...box.querySelectorAll('.ws-suggest-item')];
  items.forEach((el,i)=>{
    const active=i===idx;
    el.classList.toggle('active', active);
    if(active) el.scrollIntoView({block:'nearest'});
  });
}

function _renderWorkspacePathSuggestions(paths){
  const box=$('workspaceFormPathSuggestions');
  if(!box)return;
  box.innerHTML='';
  if(!paths || !paths.length){
    box.style.display='none';
    _wsSuggestIndex=-1;
    return;
  }
  paths.forEach((path, idx)=>{
    const pathParts=(path||'').split('/').filter(Boolean);
    const leaf=pathParts[pathParts.length-1]||path;
    const parent=pathParts.length>1?`/${pathParts.slice(0,-1).join('/')}`:'/';
    const item=document.createElement('button');
    item.type='button';
    item.className='ws-suggest-item';
    item.innerHTML=`<span class="ws-suggest-leaf">${esc(leaf)}</span><span class="ws-suggest-parent">${esc(parent)}</span>`;
    item.dataset.path=path;
    item.onmouseenter=()=>{_wsSuggestIndex=idx;_highlightWorkspaceSuggestion(idx);};
    item.onmousedown=(e)=>{e.preventDefault();_applyWorkspaceSuggestion(path);};
    box.appendChild(item);
  });
  box.style.display='block';
  _wsSuggestIndex=0;
  _highlightWorkspaceSuggestion(_wsSuggestIndex);
}

async function _loadWorkspacePathSuggestions(prefix){
  const reqId=++_wsSuggestReq;
  try{
    const qs=new URLSearchParams({prefix:prefix||''}).toString();
    const data=await api(`/api/workspaces/suggest?${qs}`);
    if(reqId!==_wsSuggestReq)return;
    _renderWorkspacePathSuggestions(data.suggestions||[]);
  }catch(_){
    if(reqId!==_wsSuggestReq)return;
    closeWorkspacePathSuggestions();
  }
}

function scheduleWorkspacePathSuggestions(){
  const input=$('workspaceFormPath');
  if(!input)return;
  const prefix=input.value.trim();
  if(!prefix){
    closeWorkspacePathSuggestions();
    return;
  }
  if(_wsSuggestTimer) clearTimeout(_wsSuggestTimer);
  _wsSuggestTimer=setTimeout(()=>{
    _loadWorkspacePathSuggestions(prefix);
  }, 120);
}

function getWorkspaceFriendlyName(path){
  // Look up the friendly name from the workspace list cache, fallback to last path segment
  if(_workspaceList && _workspaceList.length){
    const match=_workspaceList.find(w=>w.path===path);
    if(match && match.name) return match.name;
  }
  return path.split('/').filter(Boolean).pop()||path;
}

function syncWorkspaceDisplays(){
  const hasSession=!!(S.session&&S.session.workspace);
  // Fall back to the profile default workspace when no session is active yet.
  // S._profileDefaultWorkspace is set during boot and profile switches from /api/settings.
  const defaultWs=(typeof S._profileDefaultWorkspace==='string'&&S._profileDefaultWorkspace)||'';
  const ws=hasSession?S.session.workspace:(defaultWs||'');
  const hasWorkspace=!!(ws);
  const label=hasWorkspace?getWorkspaceFriendlyName(ws):t('no_workspace');

  const sidebarName=$('sidebarWsName');
  const sidebarPath=$('sidebarWsPath');
  if(sidebarName) sidebarName.textContent=label;
  if(sidebarPath) sidebarPath.textContent=ws;

  const composerChip=$('composerWorkspaceChip');
  const composerLabel=$('composerWorkspaceLabel');
  const mobileAction=$('composerMobileWorkspaceAction');
  const mobileLabel=$('composerMobileWorkspaceLabel');
  const composerDropdown=$('composerWsDropdown');
  if(!hasWorkspace && composerDropdown) composerDropdown.classList.remove('open');
  // Only show workspace label once boot has finished to prevent
  // flash of "No workspace" before the saved session finishes loading.
  if(composerLabel) composerLabel.textContent=S._bootReady?label:'';
  if(mobileLabel) mobileLabel.textContent=S._bootReady?label:'';
  if(composerChip){
    composerChip.disabled=!hasWorkspace;
    composerChip.title=hasWorkspace?ws:t('no_workspace');
    composerChip.classList.toggle('active',!!(composerDropdown&&composerDropdown.classList.contains('open')));
  }
  if(mobileAction){
    mobileAction.title=hasWorkspace?ws:t('no_workspace');
    mobileAction.classList.toggle('active',!!(composerDropdown&&composerDropdown.classList.contains('open')));
  }
}

async function loadWorkspaceList(){
  try{
    const data = await api('/api/workspaces');
    _workspaceList = data.workspaces || [];
    syncWorkspaceDisplays();
    return data;
  }catch(e){ return {workspaces:[], last:''}; }
}

function _renderWorkspaceAction(label, meta, iconSvg, onClick){
  const opt=document.createElement('div');
  opt.className='ws-opt ws-opt-action';
  opt.innerHTML=`<span class="ws-opt-icon">${iconSvg}</span><span><span class="ws-opt-name">${esc(label)}</span>${meta?`<span class="ws-opt-meta">${esc(meta)}</span>`:''}</span>`;
  opt.onclick=onClick;
  return opt;
}

function _positionComposerWsDropdown(){
  const dd=$('composerWsDropdown');
  const chip=$('composerWorkspaceGroup')||$('composerWorkspaceChip');
  const mobileAction=$('composerMobileWorkspaceAction');
  const panel=$('composerMobileConfigPanel');
  const footer=document.querySelector('.composer-footer');
  // While the mobile config panel is open, anchor to #composerMobileWorkspaceAction instead of only the desktop workspace chip.
  const anchor=(panel&&panel.classList.contains('open')&&mobileAction)?mobileAction:chip;
  if(!dd||!anchor||!footer)return;
  const chipRect=anchor.getBoundingClientRect();
  const footerRect=footer.getBoundingClientRect();
  let left=chipRect.left-footerRect.left;
  const maxLeft=Math.max(0, footer.clientWidth-dd.offsetWidth);
  left=Math.max(0, Math.min(left, maxLeft));
  dd.style.left=`${left}px`;
}

function _positionProfileDropdown(){
  const dd=$('profileDropdown');
  const chip=$('profileChip');
  const footer=document.querySelector('.composer-footer');
  if(!dd||!chip||!footer)return;
  const chipRect=chip.getBoundingClientRect();
  const footerRect=footer.getBoundingClientRect();
  let left=chipRect.left-footerRect.left;
  const maxLeft=Math.max(0, footer.clientWidth-dd.offsetWidth);
  left=Math.max(0, Math.min(left, maxLeft));
  dd.style.left=`${left}px`;
}

function renderWorkspaceDropdownInto(dd, workspaces, currentWs){
  if(!dd)return;
  dd.innerHTML='';

  // ── Search row ──────────────────────────────────────────────────────────
  const searchRow=document.createElement('div');
  searchRow.className='ws-search-row';
  searchRow.innerHTML=`<input class="ws-search-input" type="text" placeholder="${esc(t('ws_search_placeholder')||'Search workspaces…')}" spellcheck="false" autocomplete="off"><button class="ws-search-clear" title="Clear search">${li('x',10)}</button>`;
  const si=searchRow.querySelector('.ws-search-input');
  const sc=searchRow.querySelector('.ws-search-clear');
  dd.appendChild(searchRow);

  // ── Workspace list ──────────────────────────────────────────────────────
  // Sort alphabetically by name (case-insensitive) before rendering.
  const sorted=[...workspaces].sort((a,b)=>(a.name||'').localeCompare(b.name||''));
  const listContainer=document.createElement('div');
  listContainer.className='ws-list-container';
  dd.appendChild(listContainer);

  // Pre-create noResults element so filterWs can reference it safely from the start.
  const noResults=document.createElement('div');
  noResults.className='ws-no-results';
  noResults.textContent=t('ws_no_results')||'No workspaces found';
  noResults.style.display='none';

  function filterWs(term){
    term=(term||'').trim().toLowerCase();
    let visible=0;
    const opts=listContainer.querySelectorAll('.ws-opt');
    for(const opt of opts){
      const name=(opt.dataset.name||'').toLowerCase();
      const path=(opt.dataset.path||'').toLowerCase();
      const show=!term||name.includes(term)||path.includes(term);
      opt.style.display=show?'':'none';
      if(show) visible++;
    }
    noResults.style.display=visible?'none':'';
  }

  function renderList(){
    listContainer.innerHTML='';
    for(const w of sorted){
      const opt=document.createElement('div');
      opt.className='ws-opt'+(w.path===currentWs?' active':'');
      opt.dataset.name=w.name||'';
      opt.dataset.path=w.path||'';
      opt.innerHTML=`<span class="ws-opt-name">${esc(w.name)}</span><span class="ws-opt-path">${esc(w.path)}</span>`;
      opt.onclick=()=>switchToWorkspace(w.path,w.name);
      listContainer.appendChild(opt);
    }
    listContainer.appendChild(noResults);
  }

  renderList();
  filterWs('');

  si.addEventListener('input',()=>{ filterWs(si.value); });
  sc.addEventListener('click',()=>{ si.value=''; filterWs(''); si.focus(); });

  // ── Footer actions ────────────────────────────────────────────────────────
  dd.appendChild(document.createElement('div')).className='ws-divider';
  dd.appendChild(_renderWorkspaceAction(
    t('workspace_choose_path'),
    t('workspace_choose_path_meta'),
    li('folder',12),
    ()=>promptWorkspacePath()
  ));
  const div=document.createElement('div');div.className='ws-divider';dd.appendChild(div);
  dd.appendChild(_renderWorkspaceAction(
    t('workspace_manage'),
    t('workspace_manage_meta'),
    li('settings',12),
    ()=>{closeWsDropdown();mobileSwitchPanel('workspaces');}
  ));
}

function toggleWsDropdown(){
  const dd=$('wsDropdown');
  if(!dd)return;
  const open=dd.classList.contains('open');
  if(open){closeWsDropdown();}
  else{
    closeProfileDropdown(); // close profile dropdown if open
    loadWorkspaceList().then(data=>{
      renderWorkspaceDropdownInto(dd, data.workspaces, S.session?S.session.workspace:'');
      dd.classList.add('open');
    });
  }
}

function toggleComposerWsDropdown(){
  const dd=$('composerWsDropdown');
  const chip=$('composerWorkspaceChip');
  const mobileAction=$('composerMobileWorkspaceAction');
  const panel=$('composerMobileConfigPanel');
  const usingMobileAction=!!(panel&&panel.classList.contains('open')&&mobileAction);
  if(!dd||(!usingMobileAction&&(!chip||chip.disabled)))return;
  const open=dd.classList.contains('open');
  if(open){closeWsDropdown();}
  else{
    closeProfileDropdown();
    if(typeof closeModelDropdown==='function') closeModelDropdown();
    if(typeof closeReasoningDropdown==='function') closeReasoningDropdown();
    loadWorkspaceList().then(data=>{
      renderWorkspaceDropdownInto(dd, data.workspaces, S.session?S.session.workspace:'');
      dd.classList.add('open');
      _positionComposerWsDropdown();
      if(chip) chip.classList.add('active');
      if(mobileAction) mobileAction.classList.add('active');
    });
  }
}

function closeWsDropdown(){
  const dd=$('wsDropdown');
  const composerDd=$('composerWsDropdown');
  const composerChip=$('composerWorkspaceChip');
  const mobileAction=$('composerMobileWorkspaceAction');
  if(dd)dd.classList.remove('open');
  if(composerDd)composerDd.classList.remove('open');
  if(composerChip)composerChip.classList.remove('active');
  if(mobileAction)mobileAction.classList.remove('active');
}
document.addEventListener('click',e=>{
  if(
    !e.target.closest('#composerWorkspaceChip') &&
    !e.target.closest('#composerMobileWorkspaceAction') &&
    !e.target.closest('#composerWsDropdown')
  ) closeWsDropdown();
});
window.addEventListener('resize',()=>{
  const dd=$('composerWsDropdown');
  if(dd&&dd.classList.contains('open')) _positionComposerWsDropdown();
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
  const activePath = S.session ? S.session.workspace : '';
  for(let i=0;i<workspaces.length;i++){
    const w=workspaces[i];
    const row=document.createElement('div');
    row.className='ws-row';
    row.dataset.path = w.path;
    row.draggable=true;
    const isActive = w.path === activePath;
    const activeBadge = isActive ? `<span class="detail-badge active" style="margin-left:6px;font-size:9px;padding:1px 6px">${esc(t('profile_active'))}</span>` : '';
    row.innerHTML=`
      <span class="ws-drag-handle" title="${esc(t('workspace_drag_hint'))}">${li('grip-vertical',12)}</span>
      <div class="ws-row-info">
        <div class="ws-row-name">${esc(w.name)}${activeBadge}</div>
        <div class="ws-row-path">${esc(w.path)}</div>
      </div>`;
    // Click on info area only — not on drag handle
    const info=row.querySelector('.ws-row-info');
    if(info) info.onclick = (e) => { e.stopPropagation(); openWorkspaceDetail(w.path, row); };
    if (_currentWorkspaceDetail && _currentWorkspaceDetail.path === w.path) row.classList.add('active');

    // ── Drag-and-drop reorder ──
    row.addEventListener('dragstart', (e) => {
      // Only allow drag from the grip handle or the row itself
      row.classList.add('dragging');
      e.dataTransfer.effectAllowed='move';
      e.dataTransfer.setData('text/plain', w.path);
      // Required for Firefox drag ghost
      if(e.dataTransfer.setDragImage) e.dataTransfer.setDragImage(row, 0, 0);
    });
    row.addEventListener('dragend', () => {
      row.classList.remove('dragging');
      panel.querySelectorAll('.ws-row.drag-over').forEach(r => r.classList.remove('drag-over'));
    });
    row.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect='move';
      // Highlight drop target
      panel.querySelectorAll('.ws-row.drag-over').forEach(r => r.classList.remove('drag-over'));
      if(!row.classList.contains('dragging')) row.classList.add('drag-over');
    });
    row.addEventListener('dragleave', () => {
      row.classList.remove('drag-over');
    });
    row.addEventListener('drop', async (e) => {
      e.preventDefault();
      row.classList.remove('drag-over');
      const fromPath = e.dataTransfer.getData('text/plain');
      const toPath = w.path;
      if(fromPath === toPath) return; // Same item, no-op
      // Compute new order
      const currentPaths = workspaces.map(ws => ws.path);
      const fromIdx = currentPaths.indexOf(fromPath);
      const toIdx = currentPaths.indexOf(toPath);
      if(fromIdx < 0 || toIdx < 0) return;
      currentPaths.splice(fromIdx, 1);
      currentPaths.splice(toIdx, 0, fromPath);
      try {
        const res = await api('/api/workspaces/reorder', {
          method: 'POST',
          body: JSON.stringify({ paths: currentPaths })
        });
        if(res && res.ok){
          renderWorkspacesPanel(res.workspaces);
          // Also refresh sidebar dropdown
          loadWorkspaceList().then(() => {});
        }
      } catch(err){
        showToast(t('workspace_reorder_failed'), 'error');
      }
    });

    panel.appendChild(row);
  }
  const hint=document.createElement('div');
  hint.style.cssText='font-size:11px;color:var(--muted);padding:8px 0';
  hint.textContent=t('workspace_paths_validated_hint');
  panel.appendChild(hint);
  // Re-render detail if we have one cached and we're not in a form
  if (_currentWorkspaceDetail && _workspaceMode !== 'create' && _workspaceMode !== 'edit') {
    const refreshed = workspaces.find(w => w.path === _currentWorkspaceDetail.path);
    if (refreshed) _renderWorkspaceDetail(refreshed);
    else _clearWorkspaceDetail();
  }
}

function _renderWorkspaceDetail(ws){
  _currentWorkspaceDetail = ws;
  const title = $('workspaceDetailTitle');
  const body = $('workspaceDetailBody');
  const empty = $('workspaceDetailEmpty');
  if (!title || !body) return;
  title.textContent = ws.name || ws.path;
  const activePath = S.session ? S.session.workspace : '';
  const isActive = ws.path === activePath;
  const isDefault = !!ws.is_default;
  const statusBadge = isActive
    ? `<span class="detail-badge active">${esc(t('profile_active'))}</span>`
    : `<span class="detail-badge">Inactive</span>`;
  const defaultBadge = isDefault ? ` <span class="detail-badge">${esc(t('profile_default_label'))}</span>` : '';
  body.innerHTML = `
    <div class="main-view-content">
      <div class="detail-card">
        <div class="detail-card-title">Space</div>
        <div class="detail-row"><div class="detail-row-label">Name</div><div class="detail-row-value">${esc(ws.name || '')}</div></div>
        <div class="detail-row"><div class="detail-row-label">Path</div><div class="detail-row-value"><code>${esc(ws.path)}</code></div></div>
        <div class="detail-row"><div class="detail-row-label">Status</div><div class="detail-row-value">${statusBadge}${defaultBadge}</div></div>
      </div>
      <div class="detail-card" style="margin-top:12px">
        <div class="detail-card-title">${esc(t('checkpoint_title'))}</div>
        <div id="checkpointListContainer">
          <div style="color:var(--muted);font-size:12px;padding:8px 0">${esc(t('checkpoint_loading'))}</div>
        </div>
      </div>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _workspaceMode = 'read';
  _setWorkspaceHeaderButtons('read', ws);
  _loadCheckpoints(ws.path);
}

function _setWorkspaceHeaderButtons(mode, ws){
  const actBtn = $('btnActivateWorkspaceDetail');
  const editBtn = $('btnEditWorkspaceDetail');
  const delBtn = $('btnDeleteWorkspaceDetail');
  const cancelBtn = $('btnCancelWorkspaceDetail');
  const saveBtn = $('btnSaveWorkspaceDetail');
  const show = b => b && (b.style.display = '');
  const hide = b => b && (b.style.display = 'none');
  if (mode === 'read') {
    const activePath = S.session ? S.session.workspace : '';
    const isActive = ws && ws.path === activePath;
    const isDefault = !!(ws && ws.is_default);
    if (isActive) hide(actBtn); else show(actBtn);
    show(editBtn);
    if (isDefault) hide(delBtn); else show(delBtn);
    hide(cancelBtn); hide(saveBtn);
  } else if (mode === 'create' || mode === 'edit') {
    hide(actBtn); hide(editBtn); hide(delBtn); show(cancelBtn); show(saveBtn);
  } else {
    [actBtn, editBtn, delBtn, cancelBtn, saveBtn].forEach(hide);
  }
}

function openWorkspaceDetail(path, el){
  if (!_workspaceList) return;
  const ws = _workspaceList.find(w => w.path === path);
  if (!ws) return;
  document.querySelectorAll('.ws-row').forEach(e => e.classList.remove('active'));
  const target = el || document.querySelector(`.ws-row[data-path="${CSS.escape(path)}"]`);
  if (target) target.classList.add('active');
  _workspacePreFormDetail = null;
  _renderWorkspaceDetail(ws);
}

function _clearWorkspaceDetail(){
  _currentWorkspaceDetail = null;
  _workspaceMode = 'empty';
  const title = $('workspaceDetailTitle');
  const body = $('workspaceDetailBody');
  const empty = $('workspaceDetailEmpty');
  if (title) title.textContent = '';
  if (body) { body.innerHTML = ''; body.style.display = 'none'; }
  if (empty) empty.style.display = '';
  _setWorkspaceHeaderButtons('empty');
}

async function activateCurrentWorkspace(){
  if (!_currentWorkspaceDetail) return;
  await switchToWorkspace(_currentWorkspaceDetail.path, _currentWorkspaceDetail.name);
  // Re-render detail after activation so the active badge updates
  _renderWorkspaceDetail(_currentWorkspaceDetail);
}

async function deleteCurrentWorkspace(){
  if (!_currentWorkspaceDetail) return;
  const path = _currentWorkspaceDetail.path;
  const _ok = await showConfirmDialog({title:t('workspace_remove_confirm_title'),message:t('workspace_remove_confirm_message',path),confirmLabel:t('remove'),danger:true,focusCancel:true});
  if(!_ok) return;
  try{
    const data=await api('/api/workspaces/remove',{method:'POST',body:JSON.stringify({path})});
    _workspaceList=data.workspaces;
    _clearWorkspaceDetail();
    renderWorkspacesPanel(data.workspaces);
    showToast(t('workspace_removed'));
  }catch(e){setStatus(t('remove_failed')+e.message);}
}

function openWorkspaceCreate(){
  if (typeof switchPanel === 'function' && _currentPanel !== 'workspaces') switchPanel('workspaces');
  _workspacePreFormDetail = _currentWorkspaceDetail ? { ..._currentWorkspaceDetail } : null;
  _workspaceMode = 'create';
  _renderWorkspaceForm({ name:'', path:'', isEdit:false });
}

function editCurrentWorkspace(){
  if (!_currentWorkspaceDetail) return;
  _workspacePreFormDetail = { ..._currentWorkspaceDetail };
  _workspaceMode = 'edit';
  _renderWorkspaceForm({ name: _currentWorkspaceDetail.name || '', path: _currentWorkspaceDetail.path || '', isEdit: true });
}

function _renderWorkspaceForm({ name, path, isEdit }){
  const title = $('workspaceDetailTitle');
  const body = $('workspaceDetailBody');
  const empty = $('workspaceDetailEmpty');
  if (!title || !body) return;
  title.textContent = isEdit ? (t('edit') + ' · ' + (name || path)) : (t('workspace_new_title') || 'New space');
  const pathDisabled = isEdit ? 'disabled' : '';
  const pathHint = isEdit
    ? `<div class="detail-form-hint">${esc(t('workspace_path_readonly') || 'Path cannot be changed. Rename only.')}</div>`
    : `<div class="detail-form-hint">${esc(t('workspace_paths_validated_hint'))}</div>`;
  body.innerHTML = `
    <div class="main-view-content">
      <form class="detail-form" onsubmit="event.preventDefault(); saveWorkspaceForm();">
        <div class="detail-form-row">
          <label for="workspaceFormName">${esc(t('workspace_name_label') || 'Name')}</label>
          <input type="text" id="workspaceFormName" value="${esc(name || '')}" placeholder="${esc(t('workspace_name_placeholder') || 'Optional friendly name')}" autocomplete="off">
        </div>
        <div class="detail-form-row">
          <label for="workspaceFormPath">${esc(t('workspace_path_label') || 'Path')}</label>
          <div class="workspace-form-path-wrap" style="position:relative">
            <input type="text" id="workspaceFormPath" value="${esc(path || '')}" placeholder="${esc(t('workspace_add_path_placeholder') || '/absolute/path/to/folder')}" autocomplete="off" ${pathDisabled} required>
            <div id="workspaceFormPathSuggestions" class="ws-suggestions" style="display:none"></div>
          </div>
          ${pathHint}
        </div>
        <div id="workspaceFormError" class="detail-form-error" style="display:none"></div>
      </form>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _setWorkspaceHeaderButtons(isEdit ? 'edit' : 'create');
  if (!isEdit) _wireWorkspaceFormPathSuggestions();
  const focus = isEdit ? $('workspaceFormName') : $('workspaceFormPath');
  if (focus) focus.focus();
}

function cancelWorkspaceForm(){
  closeWorkspacePathSuggestions();
  if (_workspacePreFormDetail) {
    const snap = _workspacePreFormDetail;
    _workspacePreFormDetail = null;
    _renderWorkspaceDetail(snap);
    return;
  }
  _clearWorkspaceDetail();
}

async function saveWorkspaceForm(){
  const nameEl = $('workspaceFormName');
  const pathEl = $('workspaceFormPath');
  const errEl = $('workspaceFormError');
  if (!pathEl || !errEl) return;
  const name = (nameEl ? nameEl.value : '').trim();
  const path = (pathEl.value || '').trim();
  errEl.style.display = 'none';
  if (!path) { errEl.textContent = t('workspace_path_required') || 'Path is required'; errEl.style.display = ''; return; }
  try {
    if (_workspaceMode === 'edit' && _currentWorkspaceDetail) {
      const targetPath = _currentWorkspaceDetail.path;
      const newName = name || _currentWorkspaceDetail.name || '';
      await api('/api/workspaces/rename', { method:'POST', body: JSON.stringify({ path: targetPath, name: newName }) });
      // Refresh list and re-render detail
      const data = await api('/api/workspaces');
      _workspaceList = data.workspaces || [];
      _workspacePreFormDetail = null;
      showToast(t('workspace_renamed') || t('workspace_added'));
      renderWorkspacesPanel(_workspaceList);
      openWorkspaceDetail(targetPath);
      return;
    }
    const data = await api('/api/workspaces/add', { method:'POST', body: JSON.stringify({ path }) });
    _workspaceList = data.workspaces || [];
    _workspacePreFormDetail = null;
    // Apply rename if a friendly name was supplied
    if (name) {
      try { await api('/api/workspaces/rename', { method:'POST', body: JSON.stringify({ path, name }) }); } catch(_) {}
      const refreshed = await api('/api/workspaces');
      _workspaceList = refreshed.workspaces || _workspaceList;
    }
    renderWorkspacesPanel(_workspaceList);
    showToast(t('workspace_added'));
    const added = _workspaceList.find(w => w.path === path) || _workspaceList[_workspaceList.length - 1];
    if (added) openWorkspaceDetail(added.path);
  } catch (e) {
    errEl.textContent = t('error_prefix') + e.message;
    errEl.style.display = '';
  }
}

// Back-compat: any legacy caller of addWorkspace() opens the new form instead.
function addWorkspace(){ openWorkspaceCreate(); }

function _wireWorkspaceFormPathSuggestions(){
  const input=$('workspaceFormPath');
  if(!input) return;
  input.oninput=()=>scheduleWorkspacePathSuggestions();
  input.onfocus=()=>{
    if(input.value.trim()) scheduleWorkspacePathSuggestions();
    else closeWorkspacePathSuggestions();
  };
  input.onkeydown=(e)=>{
    const box=$('workspaceFormPathSuggestions');
    const items=box?[...box.querySelectorAll('.ws-suggest-item')]:[];
    if(!items.length){
      return;
    }
    if(e.key==='ArrowDown'){
      e.preventDefault();
      _wsSuggestIndex=Math.min(items.length-1,Math.max(-1,_wsSuggestIndex)+1);
      _highlightWorkspaceSuggestion(_wsSuggestIndex);
      return;
    }
    if(e.key==='ArrowUp'){
      e.preventDefault();
      _wsSuggestIndex=_wsSuggestIndex<=0?0:_wsSuggestIndex-1;
      _highlightWorkspaceSuggestion(_wsSuggestIndex);
      return;
    }
    if(e.key==='Escape'){
      e.preventDefault();
      closeWorkspacePathSuggestions();
      return;
    }
    if(e.key==='Enter' && _wsSuggestIndex>=0 && items[_wsSuggestIndex]){
      e.preventDefault();
      _applyWorkspaceSuggestion(items[_wsSuggestIndex].dataset.path||'');
      return;
    }
    if(e.key==='Tab' && _wsSuggestIndex>=0 && items[_wsSuggestIndex]){
      e.preventDefault();
      _applyWorkspaceSuggestion(items[_wsSuggestIndex].dataset.path||'');
      return;
    }
  };
}

document.addEventListener('click',e=>{
  if(!e.target.closest('.workspace-form-path-wrap')) closeWorkspacePathSuggestions();
});

async function removeWorkspace(path){
  const _rmWs=await showConfirmDialog({title:t('workspace_remove_confirm_title'),message:t('workspace_remove_confirm_message',path),confirmLabel:t('remove'),danger:true,focusCancel:true});
  if(!_rmWs) return;
  try{
    const data=await api('/api/workspaces/remove',{method:'POST',body:JSON.stringify({path})});
    _workspaceList=data.workspaces;
    renderWorkspacesPanel(data.workspaces);
    showToast(t('workspace_removed'));
  }catch(e){setStatus(t('remove_failed')+e.message);}
}

async function promptWorkspacePath(){
  // Opus review Q6: if called from blank page (no session), auto-create one first.
  if(!S.session){
    const ws=(typeof S._profileDefaultWorkspace==='string'&&S._profileDefaultWorkspace)||'';
    if(!ws)return;
    try{
      const r=await api('/api/session/new',{method:'POST',body:JSON.stringify({workspace:ws})});
      if(r&&r.session){S.session=r.session;S.messages=[];if(typeof syncTopbar==='function')syncTopbar();if(typeof renderMessages==='function')renderMessages();if(typeof renderSessionList==='function')await renderSessionList();}
    }catch(e){showToast(t('workspace_switch_failed')+e.message);return;}
    if(!S.session)return;
  }
  const value=await showPromptDialog({
    title:t('workspace_switch_prompt_title'),
    message:t('workspace_switch_prompt_message'),
    confirmLabel:t('workspace_switch_prompt_confirm'),
    placeholder:t('workspace_switch_prompt_placeholder'),
    value:S.session.workspace||''
  });
  const path=(value||'').trim();
  if(!path)return;
  try{
    const data=await api('/api/workspaces/add',{method:'POST',body:JSON.stringify({path})});
    _workspaceList=data.workspaces||[];
    const target=_workspaceList[_workspaceList.length-1];
    if(!target) throw new Error(t('workspace_not_added'));
    await switchToWorkspace(target.path,target.name);
  }catch(e){
    if(String(e.message||'').includes('Workspace already in list')){
      showToast(t('workspace_already_saved'));
      return;
    }
    showToast(t('workspace_switch_failed')+e.message);
  }
}

async function switchToWorkspace(path,name){
  // Opus review Q6: if called from blank page, auto-create a session bound to
  // the requested workspace so the switch doesn't silently no-op.
  if(!S.session){
    const ws=path||(typeof S._profileDefaultWorkspace==='string'&&S._profileDefaultWorkspace)||'';
    if(!ws){showToast(t('no_workspace'));return;}
    try{
      const r=await api('/api/session/new',{method:'POST',body:JSON.stringify({workspace:ws})});
      if(r&&r.session){S.session=r.session;S.messages=[];if(typeof syncTopbar==='function')syncTopbar();if(typeof renderMessages==='function')renderMessages();if(typeof renderSessionList==='function')await renderSessionList();}
    }catch(e){if(typeof setStatus==='function')setStatus(t('switch_failed')+e.message);return;}
    if(!S.session)return;
  }
  if(S.busy){
    showToast(t('workspace_busy_switch'));
    return;
  }
  if(typeof _previewDirty!=='undefined'&&_previewDirty){
    const discard=await showConfirmDialog({
      title:t('discard_file_edits_title'),
      message:t('discard_file_edits_message'),
      confirmLabel:t('discard'),
      danger:true
    });
    if(!discard)return;
    if(typeof cancelEditMode==='function')cancelEditMode();
    if(typeof clearPreview==='function')clearPreview();
  }
  try{
    closeWsDropdown();
    await api('/api/session/update',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id, workspace:path, model:S.session.model, model_provider:S.session.model_provider||null
    })});
    S.session.workspace=path;
    // Explicit workspace switch = user overriding any pending profile-switch default.
    // Clear the one-shot flag so a subsequent newSession() inherits this choice instead.
    S._profileSwitchWorkspace=null;
    syncTopbar();
    await loadDir('.');
    showToast(t('workspace_switched_to',name||getWorkspaceFriendlyName(path)));
  }catch(e){setStatus(t('switch_failed')+e.message);}
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
      panel.innerHTML = `<div style="padding:16px;color:var(--muted);font-size:12px">${esc(t('profiles_no_profiles'))}</div>`;
      if (_profileMode !== 'create') _clearProfileDetail();
      return;
    }
    const activeName = (S.activeProfile && data.profiles.some(p => p.name === S.activeProfile))
      ? S.activeProfile
      : (data.active || 'default');
    for (const p of data.profiles) {
      const card = document.createElement('div');
      card.className = 'profile-card';
      card.dataset.name = p.name;
      const meta = [];
      if (p.model) meta.push(p.model.split('/').pop());
      if (p.provider) meta.push(p.provider);
      if (p.skill_count) meta.push(t('profile_skill_count', p.skill_count));
      const gwDot = p.gateway_running
        ? `<span class="profile-opt-badge running" title="${esc(t('profile_gateway_running'))}"></span>`
        : `<span class="profile-opt-badge stopped" title="${esc(t('profile_gateway_stopped'))}"></span>`;
      const isActive = p.name === activeName;
      const activeBadge = isActive ? `<span style="color:var(--link);font-size:10px;font-weight:600;margin-left:6px">${esc(t('profile_active'))}</span>` : '';
      const defaultBadge = p.is_default ? ` <span style="opacity:.5">${esc(t('profile_default_label'))}</span>` : '';
      card.innerHTML = `
        <div class="profile-card-header">
          <div style="min-width:0;flex:1">
            <div class="profile-card-name${isActive ? ' is-active' : ''}">${gwDot}${esc(p.name)}${defaultBadge}${activeBadge}</div>
            ${meta.length ? `<div class="profile-card-meta">${esc(meta.join(' \u00b7 '))}</div>` : `<div class="profile-card-meta">${esc(t('profile_no_configuration'))}</div>`}
          </div>
        </div>`;
      card.onclick = () => openProfileDetail(p.name, card);
      if (_currentProfileDetail && _currentProfileDetail.name === p.name) card.classList.add('active');
      panel.appendChild(card);
    }
    // Re-render detail with fresh data if we have one and we're not in a form
    if (_currentProfileDetail && _profileMode !== 'create') {
      const refreshed = data.profiles.find(p => p.name === _currentProfileDetail.name);
      if (refreshed) _renderProfileDetail(refreshed, data.active);
      else _clearProfileDetail();
    }
  } catch (e) {
    panel.innerHTML = `<div style="color:var(--accent);font-size:12px;padding:12px">${esc(t('error_prefix'))}${esc(e.message)}</div>`;
  }
}

function _renderProfileDetail(p, activeName){
  _currentProfileDetail = p;
  const title = $('profileDetailTitle');
  const body = $('profileDetailBody');
  const empty = $('profileDetailEmpty');
  if (!title || !body) return;
  title.textContent = p.name;
  const isActive = p.name === activeName;
  const isDefault = !!p.is_default;
  const statusBadge = isActive
    ? `<span class="detail-badge active">${esc(t('profile_active'))}</span>`
    : `<span class="detail-badge">Inactive</span>`;
  const defaultBadge = isDefault ? ` <span class="detail-badge">${esc(t('profile_default_label'))}</span>` : '';
  const gwBadge = p.gateway_running
    ? `<span class="detail-badge ok">${esc(t('profile_gateway_running'))}</span>`
    : `<span class="detail-badge">${esc(t('profile_gateway_stopped'))}</span>`;
  const rows = [];
  rows.push(`<div class="detail-row"><div class="detail-row-label">Status</div><div class="detail-row-value">${statusBadge}${defaultBadge}</div></div>`);
  rows.push(`<div class="detail-row"><div class="detail-row-label">Gateway</div><div class="detail-row-value">${gwBadge}</div></div>`);
  if (p.model) rows.push(`<div class="detail-row"><div class="detail-row-label">Model</div><div class="detail-row-value"><code>${esc(p.model)}</code></div></div>`);
  if (p.provider) rows.push(`<div class="detail-row"><div class="detail-row-label">Provider</div><div class="detail-row-value">${esc(p.provider)}</div></div>`);
  if (p.base_url) rows.push(`<div class="detail-row"><div class="detail-row-label">Base URL</div><div class="detail-row-value"><code>${esc(p.base_url)}</code></div></div>`);
  rows.push(`<div class="detail-row"><div class="detail-row-label">API key</div><div class="detail-row-value">${p.has_env ? esc(t('profile_api_keys_configured')) : '<span style="color:var(--muted)">Not configured</span>'}</div></div>`);
  if (typeof p.skill_count === 'number') rows.push(`<div class="detail-row"><div class="detail-row-label">Skills</div><div class="detail-row-value">${esc(t('profile_skill_count', p.skill_count))}</div></div>`);
  if (p.default_workspace) rows.push(`<div class="detail-row"><div class="detail-row-label">Default space</div><div class="detail-row-value"><code>${esc(p.default_workspace)}</code></div></div>`);
  body.innerHTML = `
    <div class="main-view-content">
      <div class="detail-card">
        <div class="detail-card-title">Profile</div>
        ${rows.join('')}
      </div>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _profileMode = 'read';
  _setProfileHeaderButtons('read', p, activeName);
}

function _setProfileHeaderButtons(mode, p, activeName){
  const actBtn = $('btnActivateProfileDetail');
  const delBtn = $('btnDeleteProfileDetail');
  const cancelBtn = $('btnCancelProfileDetail');
  const saveBtn = $('btnSaveProfileDetail');
  const show = b => b && (b.style.display = '');
  const hide = b => b && (b.style.display = 'none');
  if (mode === 'read') {
    const isActive = p && p.name === activeName;
    const isDefault = !!(p && p.is_default);
    if (isActive) hide(actBtn); else show(actBtn);
    if (isDefault) hide(delBtn); else show(delBtn);
    hide(cancelBtn); hide(saveBtn);
  } else if (mode === 'create') {
    hide(actBtn); hide(delBtn); show(cancelBtn); show(saveBtn);
  } else {
    [actBtn, delBtn, cancelBtn, saveBtn].forEach(hide);
  }
}

function openProfileDetail(name, el){
  if (!_profilesCache || !_profilesCache.profiles) return;
  const p = _profilesCache.profiles.find(x => x.name === name);
  if (!p) return;
  document.querySelectorAll('.profile-card').forEach(e => e.classList.remove('active'));
  const target = el || document.querySelector(`.profile-card[data-name="${CSS.escape(name)}"]`);
  if (target) target.classList.add('active');
  _profilePreFormDetail = null;
  _renderProfileDetail(p, _profilesCache.active);
}

function _clearProfileDetail(){
  _currentProfileDetail = null;
  _profileMode = 'empty';
  const title = $('profileDetailTitle');
  const body = $('profileDetailBody');
  const empty = $('profileDetailEmpty');
  if (title) title.textContent = '';
  if (body) { body.innerHTML = ''; body.style.display = 'none'; }
  if (empty) empty.style.display = '';
  _setProfileHeaderButtons('empty');
}

async function activateCurrentProfile(){
  if (!_currentProfileDetail) return;
  await switchToProfile(_currentProfileDetail.name);
}

async function deleteCurrentProfile(){
  if (!_currentProfileDetail) return;
  const name = _currentProfileDetail.name;
  const _ok = await showConfirmDialog({title:t('profile_delete_confirm_title',name),message:t('profile_delete_confirm_message'),confirmLabel:t('delete_title'),danger:true,focusCancel:true});
  if(!_ok) return;
  try {
    await api('/api/profile/delete', { method: 'POST', body: JSON.stringify({ name }) });
    _clearProfileDetail();
    await loadProfilesPanel();
    showToast(t('profile_deleted', name));
  } catch (e) { showToast(t('delete_failed') + e.message); }
}

function renderProfileDropdown(data) {
  const dd = $('profileDropdown');
  if (!dd) return;
  dd.innerHTML = '';
  const profiles = data.profiles || [];
  const active = (S.activeProfile && profiles.some(p => p.name === S.activeProfile))
    ? S.activeProfile
    : (data.active || 'default');
  for (const p of profiles) {
    const opt = document.createElement('div');
    opt.className = 'profile-opt' + (p.name === active ? ' active' : '');
    const meta = [];
    if (p.model) meta.push(p.model.split('/').pop());
    if (p.skill_count) meta.push(t('profile_skill_count', p.skill_count));
    const gwDot = `<span class="profile-opt-badge ${p.gateway_running ? 'running' : 'stopped'}"></span>`;
    const checkmark = p.name === active ? ' <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--link)" stroke-width="3" style="vertical-align:-1px"><polyline points="20 6 9 17 4 12"/></svg>' : '';
    const defaultBadge = p.is_default ? ` <span style="opacity:.5;font-weight:400">${esc(t('profile_default_label'))}</span>` : '';
    opt.innerHTML = `<div class="profile-opt-name">${gwDot}${esc(p.name)}${defaultBadge}${checkmark}</div>` +
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
  mgmt.innerHTML = `${li('settings',12)} ${esc(t('manage_profiles'))}`;
  mgmt.onclick = () => { closeProfileDropdown(); mobileSwitchPanel('profiles'); };
  dd.appendChild(mgmt);
}

function toggleProfileDropdown() {
  const dd = $('profileDropdown');
  if (!dd) return;
  if (dd.classList.contains('open')) { closeProfileDropdown(); return; }
  closeWsDropdown(); // close workspace dropdown if open
  if(typeof closeModelDropdown==='function') closeModelDropdown();
  api('/api/profiles').then(data => {
    renderProfileDropdown(data);
    dd.classList.add('open');
    _positionProfileDropdown();
    const chip=$('profileChip');
    if(chip) chip.classList.add('active');
  }).catch(e => { showToast(t('profiles_load_failed')); });
}

function closeProfileDropdown() {
  const dd = $('profileDropdown');
  if (dd) dd.classList.remove('open');
  const chip=$('profileChip');
  if(chip) chip.classList.remove('active');
}
document.addEventListener('click', e => {
  if (!e.target.closest('#profileChipWrap') && !e.target.closest('#profileDropdown')) closeProfileDropdown();
});
window.addEventListener('resize',()=>{
  const dd=$('profileDropdown');
  if(dd&&dd.classList.contains('open')) _positionProfileDropdown();
});

async function switchToProfile(name) {
  if (S.busy) { showToast(t('profiles_busy_switch')); return; }

  // ── Loading indicator ───────────────────────────────────────────────────
  // Show spinner on the profile chip immediately so the user gets visual
  // feedback while the async switch is in progress.
  const _chip = $('profileChip');
  const _chipLabel = $('profileChipLabel');
  const _prevProfileName = S.activeProfile || 'default';
  if (_chip) { _chip.classList.add('switching'); _chip.disabled = true; }
  // Optimistic name update — shows the target name right away
  if (_chipLabel) _chipLabel.textContent = name;

  // Determine whether the current session has any messages.
  // A session with messages is "in progress" and belongs to the current profile —
  // we must not retag it.  We'll start a fresh session for the new profile instead.
  const sessionInProgress = S.session && S.messages && S.messages.length > 0;

  try {
    const data = await api('/api/profile/switch', { method: 'POST', body: JSON.stringify({ name }) });
    S.activeProfile = data.active || name;

    // Update composer placeholder and title bar while the core profile-switch
    // state is still close to the profile API response.
    if (typeof applyBotName === 'function') applyBotName();

    // ── Model + Workspace (parallelized) ───────────────────────────────────
    // populateModelDropdown hits /api/models; loadWorkspaceList hits /api/workspaces.
    // They are fully independent — run both simultaneously to cut switch time ~50%.
    if(typeof _clearPersistedModelState==='function') _clearPersistedModelState();
    else localStorage.removeItem('hermes-webui-model');
    _skillsData = null;
    _workspaceList = null;
    await Promise.all([populateModelDropdown(), loadWorkspaceList()]);

    // ── Apply model ────────────────────────────────────────────────────────
    if (data.default_model) {
      const sel = $('modelSelect');
      const resolved = _applyModelToDropdown(data.default_model, sel, window._activeProvider||null);
      const modelToUse = resolved || data.default_model;
      const modelState = (typeof _modelStateForSelect==='function')
        ? _modelStateForSelect(sel, modelToUse)
        : {model:modelToUse,model_provider:null};
      S._pendingProfileModel = modelToUse;
      S._pendingProfileModelProvider = modelState.model_provider||null;
      // Only patch the in-memory session model if we're NOT about to replace the session
      if (S.session && !sessionInProgress) {
        S.session.model = modelToUse;
        S.session.model_provider = modelState.model_provider||null;
      }
    }

    // ── Apply workspace ────────────────────────────────────────────────────
    if (data.default_workspace) {
      // Always store the persistent profile default — used for blank-page display
      // and workspace auto-bind throughout the session lifecycle (#804, #823).
      S._profileDefaultWorkspace = data.default_workspace;
      // Also set the one-shot flag consumed by newSession() so the first new
      // session after a profile switch inherits this workspace (#424).
      S._profileSwitchWorkspace = data.default_workspace;

      if (S.session && !sessionInProgress) {
        // Empty session (no messages yet) — safe to update it in place
        try {
          await api('/api/session/update', { method: 'POST', body: JSON.stringify({
            session_id: S.session.session_id,
            workspace: data.default_workspace,
            model: S.session.model,
            model_provider: S.session.model_provider||null,
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
      // Apply profile default workspace to the newly created session (fixes #424)
      if (S._profileDefaultWorkspace && S.session) {
        try {
          await api('/api/session/update', { method: 'POST', body: JSON.stringify({
            session_id: S.session.session_id,
            workspace: S._profileDefaultWorkspace,
            model: S.session.model,
            model_provider: S.session.model_provider||null,
          })});
          S.session.workspace = S._profileDefaultWorkspace;
        } catch (_) {}
      }
      // Keep topbar chips (workspace/profile) in sync after creating the
      // new profile-scoped session.
      syncTopbar();
      await renderSessionList();
      showToast(t('profile_switched_new_conversation', name));
    } else {
      // No messages yet — just refresh the list and topbar in place
      await renderSessionList();
      syncTopbar();
      // Refresh workspace file tree so the right panel shows the new
      // profile's workspace, not the previous one (#1214).
      if (S.session && S.session.workspace) loadDir('.');
      showToast(t('profile_switched', name));
    }

    // ── Sidebar panels ─────────────────────────────────────────────────────
    if (_currentPanel === 'skills') await loadSkills();
    if (_currentPanel === 'memory') await loadMemory();
    if (_currentPanel === 'tasks') await loadCrons();
    if (_currentPanel === 'kanban') await loadKanban();
    if (_currentPanel === 'profiles') await loadProfilesPanel();
    if (_currentPanel === 'workspaces') await loadWorkspacesPanel();

  } catch (e) {
    // Revert the optimistic name update on error
    if (_chipLabel) _chipLabel.textContent = _prevProfileName;
    showToast(t('switch_failed') + e.message);
  } finally {
    // Always remove loading indicator regardless of success or failure
    if (_chip) { _chip.classList.remove('switching'); _chip.disabled = false; }
  }
}

function openProfileCreate(){
  if (typeof switchPanel === 'function' && _currentPanel !== 'profiles') switchPanel('profiles');
  _profilePreFormDetail = _currentProfileDetail ? { ..._currentProfileDetail } : null;
  _profileMode = 'create';
  _renderProfileForm();
}

function _renderProfileForm(){
  const title = $('profileDetailTitle');
  const body = $('profileDetailBody');
  const empty = $('profileDetailEmpty');
  if (!title || !body) return;
  title.textContent = t('new_profile');
  body.innerHTML = `
    <div class="main-view-content">
      <form class="detail-form" onsubmit="event.preventDefault(); saveProfileForm();">
        <div class="detail-form-row">
          <label for="profileFormName">${esc(t('profile_name_label') || 'Name')}</label>
          <input type="text" id="profileFormName" placeholder="${esc(t('profile_name_placeholder') || 'lowercase, a-z 0-9 hyphens')}" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" required>
          <div class="detail-form-hint">${esc(t('profile_name_rule') || 'Lowercase letters, numbers, hyphens, underscores only.')}</div>
        </div>
        <div class="detail-form-row">
          <label class="detail-form-check" for="profileFormClone">
            <input type="checkbox" id="profileFormClone"> <span>${esc(t('profile_clone_label') || 'Clone config from active profile')}</span>
          </label>
        </div>
        <div class="detail-form-row">
          <label for="profileFormBaseUrl">${esc(t('profile_base_url_label') || 'Base URL')}</label>
          <input type="text" id="profileFormBaseUrl" placeholder="${esc(t('profile_base_url_placeholder') || 'Optional, e.g. http://localhost:11434')}" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false">
        </div>
        <div class="detail-form-row">
          <label for="profileFormApiKey">${esc(t('profile_api_key_label') || 'API key')}</label>
          <input type="password" id="profileFormApiKey" placeholder="${esc(t('profile_api_key_placeholder') || 'Optional')}" autocomplete="off">
        </div>
        <div id="profileFormError" class="detail-form-error" style="display:none"></div>
      </form>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _setProfileHeaderButtons('create');
  const n = $('profileFormName');
  if (n) n.focus();
}

function cancelProfileForm(){
  if (_profilePreFormDetail) {
    const snap = _profilePreFormDetail;
    _profilePreFormDetail = null;
    const activeName = _profilesCache ? _profilesCache.active : null;
    _renderProfileDetail(snap, activeName);
    return;
  }
  _clearProfileDetail();
}

async function saveProfileForm(){
  const nameEl = $('profileFormName');
  const cloneEl = $('profileFormClone');
  const baseEl = $('profileFormBaseUrl');
  const apiKeyEl = $('profileFormApiKey');
  const errEl = $('profileFormError');
  if (!nameEl || !errEl) return;
  const name = (nameEl.value || '').trim().toLowerCase();
  const cloneConfig = !!(cloneEl && cloneEl.checked);
  errEl.style.display = 'none';
  if (!name) { errEl.textContent = t('name_required'); errEl.style.display = ''; return; }
  if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(name)) { errEl.textContent = t('profile_name_rule'); errEl.style.display = ''; return; }
  const baseUrl = (baseEl ? (baseEl.value || '') : '').trim();
  const apiKey = (apiKeyEl ? (apiKeyEl.value || '') : '').trim();
  if (baseUrl && !/^https?:\/\//.test(baseUrl)) { errEl.textContent = t('profile_base_url_rule'); errEl.style.display = ''; return; }
  try {
    const payload = { name, clone_config: cloneConfig };
    if (baseUrl) payload.base_url = baseUrl;
    if (apiKey) payload.api_key = apiKey;
    await api('/api/profile/create', { method: 'POST', body: JSON.stringify(payload) });
    _profilePreFormDetail = null;
    await loadProfilesPanel();
    showToast(t('profile_created', name));
    openProfileDetail(name);
  } catch (e) {
    errEl.textContent = e.message || t('create_failed');
    errEl.style.display = '';
  }
}

// Back-compat
const submitProfileCreate = saveProfileForm;
function toggleProfileForm(){ openProfileCreate();
}

async function deleteProfile(name) {
  const _delProf=await showConfirmDialog({title:t('profile_delete_confirm_title',name),message:t('profile_delete_confirm_message'),confirmLabel:t('delete_title'),danger:true,focusCancel:true});
  if(!_delProf) return;
  try {
    await api('/api/profile/delete', { method: 'POST', body: JSON.stringify({ name }) });
    await loadProfilesPanel();
    showToast(t('profile_deleted', name));
  } catch (e) { showToast(t('delete_failed') + e.message); }
}

// ── Memory panel ──
async function loadMemory(force) {
  const panel = $('memoryPanel');
  try {
    const data = await api('/api/memory');
    _memoryData = data;
    if (panel) {
      panel.innerHTML = '';
      for (const s of MEMORY_SECTIONS) {
        const el = document.createElement('button');
        el.type = 'button';
        el.className = 'side-menu-item';
        if (_currentMemorySection === s.key) el.classList.add('active');
        el.innerHTML = `${li(s.iconKey,16)}<span>${esc(t(s.labelKey))}</span>`;
        el.onclick = () => openMemorySection(s.key, el);
        panel.appendChild(el);
      }
    }
    if (_currentMemorySection && _memoryMode !== 'edit') {
      _renderMemoryDetail(_currentMemorySection);
    }
  } catch(e) {
    if (panel) panel.innerHTML = `<div style="padding:12px;color:var(--accent);font-size:12px">${esc(t('error_prefix'))}${esc(e.message)}</div>`;
  }
}

// Drag and drop
const wrap=$('composerWrap');let dragCounter=0;
document.addEventListener('dragover',e=>e.preventDefault());
document.addEventListener('dragenter',e=>{e.preventDefault();if(e.dataTransfer.types.includes('Files')||e.dataTransfer.types.includes('application/ws-path')){dragCounter++;wrap.classList.add('drag-over');}});
document.addEventListener('dragleave',e=>{dragCounter--;if(dragCounter<=0){dragCounter=0;wrap.classList.remove('drag-over');}});
document.addEventListener('drop',e=>{
  e.preventDefault();dragCounter=0;wrap.classList.remove('drag-over');
  // Workspace file/folder drag → insert @path reference into composer
  const wsPath=e.dataTransfer.getData('application/ws-path');
  if(wsPath){
    const msgEl=$('msg');
    if(msgEl){
      const start=msgEl.selectionStart;const end=msgEl.selectionEnd;
      const val=msgEl.value;
      const prefix=start>0&&!val[start-1].match(/\s/)?' ':'';
      const insert=prefix+'@'+wsPath+' ';
      msgEl.value=val.slice(0,start)+insert+val.slice(end);
      msgEl.selectionStart=msgEl.selectionEnd=start+insert.length;
      msgEl.focus();
    }
    return;
  }
  // OS file drag → attach files
  const files=Array.from(e.dataTransfer.files);
  if(files.length){addFiles(files);$('msg').focus();}
});

// ── Settings panel ───────────────────────────────────────────────────────────

let _settingsDirty = false;
let _settingsThemeOnOpen = null; // track theme at open time for discard revert
let _settingsSkinOnOpen = null; // track skin at open time for discard revert
let _settingsFontSizeOnOpen = null; // track font size at open time for discard revert
let _settingsHermesDefaultModelOnOpen = '';
let _settingsSection = 'conversation';
let _currentSettingsSection = 'conversation';
let _settingsAppearanceAutosaveTimer = null;
let _settingsAppearanceAutosaveRetryPayload = null;
let _settingsPreferencesAutosaveTimer = null;
let _settingsPreferencesAutosaveRetryPayload = null;

function switchSettingsSection(name){
  const section=(name==='appearance'||name==='preferences'||name==='providers'||name==='plugins'||name==='system')?name:'conversation';
  _settingsSection=section;
  _currentSettingsSection=section;
  const map={conversation:'Conversation',appearance:'Appearance',preferences:'Preferences',providers:'Providers',plugins:'Plugins',system:'System'};
  // Sidebar menu items
  document.querySelectorAll('#settingsMenu .side-menu-item').forEach(it=>{
    it.classList.toggle('active', it.dataset.settingsSection===section);
  });
  // Panes in main
  ['conversation','appearance','preferences','providers','plugins','system'].forEach(key=>{
    const pane=$('settingsPane'+map[key]);
    if(pane) pane.classList.toggle('active', key===section);
  });
  // Sync mobile dropdown
  const dd=$('settingsSectionDropdown');
  if(dd && dd.value!==section) dd.value=section;
  // Lazy-load integration panels when their tabs are opened
  if(section==='providers') loadProvidersPanel();
  if(section==='plugins') loadPluginsPanel();
}

function _syncHermesPanelSessionActions(){
  const hasSession=!!S.session;
  const visibleMessages=hasSession?(S.messages||[]).filter(m=>m&&m.role&&m.role!=='tool').length:0;
  const title=hasSession?(S.session.title||t('untitled')):t('active_conversation_none');
  const meta=$('hermesSessionMeta');
  if(meta){
    meta.textContent=hasSession
      ? t('active_conversation_meta', title, visibleMessages)
      : t('active_conversation_none');
  }
  const setDisabled=(id,disabled)=>{
    const el=$(id);
    if(!el)return;
    el.disabled=!!disabled;
    el.classList.toggle('disabled',!!disabled);
  };
  setDisabled('btnDownload',!hasSession||visibleMessages===0);
  setDisabled('btnExportJSON',!hasSession);
  setDisabled('btnClearConvModal',!hasSession||visibleMessages===0);
}

// Thin wrapper: settings now live in the main content area. External callers
// (keyboard shortcuts, commands) keep working through this name.
function toggleSettings(){
  if(_currentPanel==='settings'){
    _closeSettingsPanel();
  } else {
    switchPanel('settings');
  }
}

function _resetSettingsPanelState(){
  const bar=$('settingsUnsavedBar');
  if(bar) bar.style.display='none';
  _setAppearanceAutosaveStatus('');
}

function _hideSettingsPanel(){
  _resetSettingsPanelState();
  const target = _consumeSettingsTargetPanel('chat');
  if(_currentPanel==='settings') switchPanel(target, {bypassSettingsGuard:true});
}

// Close with unsaved-changes check. If dirty, show a confirm dialog.
function _closeSettingsPanel(){
  if(!_settingsDirty){
    _revertSettingsPreview();
    _hideSettingsPanel();
    return;
  }
  _pendingSettingsTargetPanel = _pendingSettingsTargetPanel || 'chat';
  _showSettingsUnsavedBar();
}

// Revert live DOM/localStorage to what they were when the panel opened
function _revertSettingsPreview(){
  // Appearance controls autosave immediately. Closing/discarding the settings
  // panel must not roll back theme, skin, or font-size after the user sees the
  // inline saved state.
}

// Show the "Unsaved changes" bar inside the settings panel
function _showSettingsUnsavedBar(){
  let bar = $('settingsUnsavedBar');
  if(bar){ bar.style.display=''; return; }
  // Create it
  bar = document.createElement('div');
  bar.id = 'settingsUnsavedBar';
  bar.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px;background:rgba(233,69,96,.12);border:1px solid rgba(233,69,96,.3);border-radius:8px;padding:10px 14px;margin:0 0 12px;font-size:13px;';
  bar.innerHTML = `<span style="color:var(--text)">${esc(t('settings_unsaved_changes'))}</span>`
    + '<span style="display:flex;gap:8px">'
    + `<button onclick="_discardSettings()" style="padding:5px 12px;border-radius:6px;border:1px solid var(--border2);background:rgba(255,255,255,.06);color:var(--muted);cursor:pointer;font-size:12px;font-weight:600">${esc(t('discard'))}</button>`
    + `<button onclick="saveSettings(true)" style="padding:5px 12px;border-radius:6px;border:none;background:var(--accent);color:#fff;cursor:pointer;font-size:12px;font-weight:600">${esc(t('save'))}</button>`
    + '</span>';
  const body = document.querySelector('#mainSettings .settings-main') || document.querySelector('.settings-main');
  if(body) body.prepend(bar);
}

function _discardSettings(){
  _revertSettingsPreview();
  _settingsDirty = false;
  _hideSettingsPanel();
}

// Mark settings as dirty whenever anything changes
function _markSettingsDirty(){
  _settingsDirty = true;
}

// Apply TTS enabled state: toggles a body class so the CSS rule
// `body.tts-enabled .msg-tts-btn` shows/hides the speaker icon. We toggle the
// body class instead of writing inline `style.display` because the parent
// `.msg-action-btn` has no display rule, so clearing the inline style let the
// `.msg-tts-btn{display:none;}` cascade re-hide the button (#1409).
function _applyTtsEnabled(enabled){
  document.body.classList.toggle('tts-enabled', !!enabled);
}

function _appearancePayloadFromUi(){
  return {
    theme: ($('settingsTheme')||{}).value || localStorage.getItem('hermes-theme') || 'dark',
    skin: ($('settingsSkin')||{}).value || localStorage.getItem('hermes-skin') || 'default',
    font_size: ($('settingsFontSize')||{}).value || localStorage.getItem('hermes-font-size') || 'default',
  };
}

function _setAppearanceAutosaveStatus(state){
  const el=$('settingsAppearanceAutosaveStatus');
  if(!el) return;
  el.className='settings-autosave-status';
  if(!state){
    el.textContent='';
    return;
  }
  el.classList.add('is-'+state);
  if(state==='saving'){
    el.textContent=t('settings_autosave_saving');
  }else if(state==='saved'){
    el.textContent=t('settings_autosave_saved');
  }else if(state==='failed'){
    el.innerHTML=`<span>${esc(t('settings_autosave_failed'))}</span> <button type="button" onclick="_retryAppearanceAutosave()">${esc(t('settings_autosave_retry'))}</button>`;
  }
}

function _rememberAppearanceSaved(payload){
  if(!payload) return;
  _settingsThemeOnOpen=payload.theme||localStorage.getItem('hermes-theme')||'dark';
  _settingsSkinOnOpen=payload.skin||localStorage.getItem('hermes-skin')||'default';
  _settingsFontSizeOnOpen=payload.font_size||localStorage.getItem('hermes-font-size')||'default';
}

function _scheduleAppearanceAutosave(){
  const payload=_appearancePayloadFromUi();
  // Keep discard/close behavior aligned with the new mental model: appearance
  // changes are committed immediately instead of treated as preview-only edits.
  _rememberAppearanceSaved(payload);
  _settingsAppearanceAutosaveRetryPayload=payload;
  _setAppearanceAutosaveStatus('saving');
  if(_settingsAppearanceAutosaveTimer) clearTimeout(_settingsAppearanceAutosaveTimer);
  _settingsAppearanceAutosaveTimer=setTimeout(()=>_autosaveAppearanceSettings(payload),350);
}

async function _autosaveAppearanceSettings(payload){
  try{
    const saved=await api('/api/settings',{method:'POST',body:JSON.stringify(payload)});
    _settingsAppearanceAutosaveRetryPayload=null;
    _rememberAppearanceSaved(payload);
    if(saved&&saved.font_size){
      localStorage.setItem('hermes-font-size',saved.font_size);
    }
    _setAppearanceAutosaveStatus('saved');
  }catch(e){
    console.warn('[settings] appearance autosave failed', e);
    _setAppearanceAutosaveStatus('failed');
  }
}

function _retryAppearanceAutosave(){
  const payload=_settingsAppearanceAutosaveRetryPayload||_appearancePayloadFromUi();
  _setAppearanceAutosaveStatus('saving');
  _autosaveAppearanceSettings(payload);
}

// ── Phase 2: Preferences autosave (Issue #1003) ───────────────────────

function _preferencesPayloadFromUi(){
  const payload={};
  const sendKeySel=$('settingsSendKey');
  if(sendKeySel) payload.send_key=sendKeySel.value;
  const langSel=$('settingsLanguage');
  if(langSel) payload.language=langSel.value;
  const showUsageCb=$('settingsShowTokenUsage');
  if(showUsageCb) payload.show_token_usage=showUsageCb.checked;
  const showTpsCb=$('settingsShowTps');
  if(showTpsCb) payload.show_tps=showTpsCb.checked;
  const simplifiedToolCb=$('settingsSimplifiedToolCalling');
  if(simplifiedToolCb) payload.simplified_tool_calling=simplifiedToolCb.checked;
  const apiRedactCb=$('settingsApiRedact');
  if(apiRedactCb) payload.api_redact_enabled=apiRedactCb.checked;
  const showCliCb=$('settingsShowCliSessions');
  if(showCliCb) payload.show_cli_sessions=showCliCb.checked;
  const syncCb=$('settingsSyncInsights');
  if(syncCb) payload.sync_to_insights=syncCb.checked;
  const updateCb=$('settingsCheckUpdates');
  if(updateCb) payload.check_for_updates=updateCb.checked;
  const soundCb=$('settingsSoundEnabled');
  if(soundCb) payload.sound_enabled=soundCb.checked;
  const notifCb=$('settingsNotificationsEnabled');
  if(notifCb) payload.notifications_enabled=notifCb.checked;
  const sidebarDensitySel=$('settingsSidebarDensity');
  if(sidebarDensitySel) payload.sidebar_density=sidebarDensitySel.value;
  const autoTitleRefreshSel=$('settingsAutoTitleRefresh');
  if(autoTitleRefreshSel) payload.auto_title_refresh_every=parseInt(autoTitleRefreshSel.value,10);
  const busyInputModeSel=$('settingsBusyInputMode');
  if(busyInputModeSel) payload.busy_input_mode=busyInputModeSel.value;
  const botNameField=$('settingsBotName');
  if(botNameField) payload.bot_name=botNameField.value;
  return payload;
}

function _setPreferencesAutosaveStatus(state){
  const el=$('settingsPreferencesAutosaveStatus');
  if(!el) return;
  el.className='settings-autosave-status';
  if(!state){
    el.textContent='';
    return;
  }
  el.classList.add('is-'+state);
  if(state==='saving'){
    el.textContent=t('settings_autosave_saving');
  }else if(state==='saved'){
    el.textContent=t('settings_autosave_saved');
  }else if(state==='failed'){
    el.innerHTML=`<span>${esc(t('settings_autosave_failed'))}</span> <button type=\"button\" onclick=\"_retryPreferencesAutosave()\">${esc(t('settings_autosave_retry'))}</button>`;
  }
}

function _rememberPreferencesSaved(payload){
  if(!payload) return;
  if(payload.send_key!==undefined) localStorage.setItem('hermes-pref-send_key',payload.send_key);
  if(payload.language!==undefined) localStorage.setItem('hermes-pref-language',payload.language);
}

function _schedulePreferencesAutosave(){
  const payload=_preferencesPayloadFromUi();
  _rememberPreferencesSaved(payload);
  _settingsPreferencesAutosaveRetryPayload=payload;
  _setPreferencesAutosaveStatus('saving');
  if(_settingsPreferencesAutosaveTimer) clearTimeout(_settingsPreferencesAutosaveTimer);
  _settingsPreferencesAutosaveTimer=setTimeout(()=>_autosavePreferencesSettings(payload),350);
}

async function _autosavePreferencesSettings(payload){
  try{
    const saved=await api('/api/settings',{method:'POST',body:JSON.stringify(payload)});
    if(payload&&payload.simplified_tool_calling!==undefined){
      window._simplifiedToolCalling=(saved&&saved.simplified_tool_calling!==false);
      if(typeof clearMessageRenderCache==='function') clearMessageRenderCache();
      if(typeof renderMessages==='function') renderMessages();
    }
    if(payload&&payload.show_tps!==undefined){
      window._showTps=!!(saved&&saved.show_tps);
      if(typeof clearMessageRenderCache==='function') clearMessageRenderCache();
      if(typeof renderMessages==='function') renderMessages();
    }
    _settingsPreferencesAutosaveRetryPayload=null;
    _setPreferencesAutosaveStatus('saved');
    // Only clear the global dirty flag and hide the unsaved-changes bar when
    // there is no pending edit on a manually-saved field. Password and model
    // are still committed via the explicit "Save Settings" button (password
    // for security; model goes through /api/default-model). Without this
    // guard, autosaving a checkbox right after a user typed in the password
    // field would silently dismiss the password edit. (Opus pre-release
    // review of v0.50.250, SHOULD-FIX Q1.)
    const pwField=$('settingsPassword');
    const pwDirty=!!(pwField&&pwField.value);
    const modelSel=$('settingsModel');
    const modelDirty=!!(modelSel&&((modelSel.value||'')!==(_settingsHermesDefaultModelOnOpen||'')));
    if(!pwDirty&&!modelDirty){
      _settingsDirty=false;
      const bar=$('settingsUnsavedBar');
      if(bar) bar.style.display='none';
    }
  }catch(e){
    console.warn('[settings] preferences autosave failed', e);
    _setPreferencesAutosaveStatus('failed');
  }
}

function _retryPreferencesAutosave(){
  const payload=_settingsPreferencesAutosaveRetryPayload||_preferencesPayloadFromUi();
  _setPreferencesAutosaveStatus('saving');
  _autosavePreferencesSettings(payload);
}

async function loadSettingsPanel(){
  try{
    const settings=await api('/api/settings');
    // Populate the version badges from the server — keeps them in sync with git
    // tags automatically without any manual release step.
    const webuiBadge = $('settings-webui-version-badge');
    if(webuiBadge){
      webuiBadge.textContent = `WebUI: ${settings.webui_version || 'not detected'}`;
    }
    const agentBadge = $('settings-agent-version-badge');
    if(agentBadge){
      const agentVersion = (settings.agent_version || 'not detected').toString().trim() || 'not detected';
      agentBadge.textContent = `Agent: ${agentVersion}`;
    }
    // Hydrate appearance controls first so a slow /api/models request
    // cannot overwrite an in-progress theme/skin selection.
    const themeSel=$('settingsTheme');
    const themeVal=settings.theme||'dark';
    if(themeSel) themeSel.value=themeVal;
    if(typeof _syncThemePicker==='function') _syncThemePicker(themeVal);
    const skinVal=(settings.skin||'default').toLowerCase();
    const skinSel=$('settingsSkin');
    if(skinSel) skinSel.value=skinVal;
    if(typeof _buildSkinPicker==='function') _buildSkinPicker(skinVal);
    const fontSizeVal=settings.font_size||localStorage.getItem('hermes-font-size')||'default';
    localStorage.setItem('hermes-font-size',fontSizeVal);
    if(typeof _applyFontSize==='function') _applyFontSize(fontSizeVal);
    const fontSizeSel=$('settingsFontSize');
    if(fontSizeSel) fontSizeSel.value=fontSizeVal;
    if(typeof _syncFontSizePicker==='function') _syncFontSizePicker(fontSizeVal);
    // Workspace panel default-open toggle (localStorage-backed)
    // Uses a separate key (hermes-webui-workspace-panel-pref) so that
    // closing the panel via toolbar X does not clear the user's preference.
    const wsPanelCb=$('settingsWorkspacePanelOpen');
    if(wsPanelCb){
      wsPanelCb.checked=localStorage.getItem('hermes-webui-workspace-panel-pref')==='open';
      wsPanelCb.onchange=function(){
        const open=this.checked;
        localStorage.setItem('hermes-webui-workspace-panel-pref',open?'open':'closed');
        // Also sync the runtime key so the current session reflects the change
        localStorage.setItem('hermes-webui-workspace-panel',open?'open':'closed');
        document.documentElement.dataset.workspacePanel=open?'open':'closed';
        if(open&&_workspacePanelMode==='closed') openWorkspacePanel('browse');
        else if(!open&&_workspacePanelMode!=='closed') toggleWorkspacePanel(false);
      };
    }
    const resolvedLanguage=(typeof resolvePreferredLocale==='function')
      ? resolvePreferredLocale(settings.language, localStorage.getItem('hermes-lang'))
      : (settings.language || localStorage.getItem('hermes-lang') || 'en');
    // Keep settings modal and current page strings in sync with the resolved locale.
    if(typeof setLocale==='function'){
      setLocale(resolvedLanguage);
      if(typeof applyLocaleToDOM==='function') applyLocaleToDOM();
    }
    // Populate model dropdown from /api/models + live model fetch (#872)
    const modelSel=$('settingsModel');
    if(modelSel){
      modelSel.innerHTML='';
      let models=null;
      try{
        models=await api('/api/models');
        for(const g of ((models||{}).groups||[])){
          const og=document.createElement('optgroup');
          og.label=g.provider;
          if(g.provider_id) og.dataset.provider=g.provider_id;
          for(const m of g.models){
            const opt=document.createElement('option');
            opt.value=m.id;opt.textContent=m.label;
            og.appendChild(opt);
          }
          modelSel.appendChild(og);
        }
        // Append live-fetched models for the active provider, same as the
        // chat-header dropdown does via _fetchLiveModels() (#872).
        if(models.active_provider && typeof _fetchLiveModels==='function'){
          _fetchLiveModels(models.active_provider, modelSel);
        }
      }catch(e){}
      _settingsHermesDefaultModelOnOpen=(models&&models.default_model)||'';
      // Use the smart matcher so a saved bare form like "anthropic/claude-opus-4.6"
      // (what the CLI's `hermes model` command writes) still selects the matching
      // `@nous:anthropic/claude-opus-4.6` option on a Nous setup. Without this, the
      // picker renders blank for any user whose default was persisted without the
      // @-prefix — CLI-first users, legacy installs, etc.
      if(typeof _applyModelToDropdown==='function'){
        _applyModelToDropdown(_settingsHermesDefaultModelOnOpen, modelSel, (models&&models.active_provider)||window._activeProvider||null);
      }else{
        modelSel.value=_settingsHermesDefaultModelOnOpen;
      }
      modelSel.addEventListener('change',_markSettingsDirty,{once:false});
    }
    // Send key preference
    const sendKeySel=$('settingsSendKey');
    if(sendKeySel){sendKeySel.value=settings.send_key||'enter';sendKeySel.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    // Language preference — populate from LOCALES bundle
    const langSel=$('settingsLanguage');
    if(langSel){
      langSel.innerHTML='';
      if(typeof LOCALES!=='undefined'){
        for(const [code,bundle] of Object.entries(LOCALES)){
          const opt=document.createElement('option');
          opt.value=code;opt.textContent=bundle._label||code;
          langSel.appendChild(opt);
        }
      }
      langSel.value=resolvedLanguage;
      langSel.addEventListener('change',_schedulePreferencesAutosave,{once:false});
    }
    const showUsageCb=$('settingsShowTokenUsage');
    if(showUsageCb){showUsageCb.checked=!!settings.show_token_usage;showUsageCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const showTpsCb=$('settingsShowTps');
    if(showTpsCb){showTpsCb.checked=!!settings.show_tps;showTpsCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const simplifiedToolCb=$('settingsSimplifiedToolCalling');
    if(simplifiedToolCb){simplifiedToolCb.checked=settings.simplified_tool_calling!==false;simplifiedToolCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const apiRedactCb=$('settingsApiRedact');
    if(apiRedactCb){apiRedactCb.checked=settings.api_redact_enabled!==false;apiRedactCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const showCliCb=$('settingsShowCliSessions');
    if(showCliCb){showCliCb.checked=!!settings.show_cli_sessions;showCliCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const syncCb=$('settingsSyncInsights');
    if(syncCb){syncCb.checked=!!settings.sync_to_insights;syncCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const updateCb=$('settingsCheckUpdates');
    if(updateCb){updateCb.checked=settings.check_for_updates!==false;updateCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const soundCb=$('settingsSoundEnabled');
    if(soundCb){soundCb.checked=!!settings.sound_enabled;soundCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    // TTS settings (localStorage-only, no server round-trip needed)
    const ttsEnabledCb=$('settingsTtsEnabled');
    if(ttsEnabledCb){ttsEnabledCb.checked=localStorage.getItem('hermes-tts-enabled')==='true';ttsEnabledCb.onchange=function(){localStorage.setItem('hermes-tts-enabled',this.checked?'true':'false');_applyTtsEnabled(this.checked);};}
    const ttsAutoReadCb=$('settingsTtsAutoRead');
    if(ttsAutoReadCb){ttsAutoReadCb.checked=localStorage.getItem('hermes-tts-auto-read')==='true';ttsAutoReadCb.onchange=function(){localStorage.setItem('hermes-tts-auto-read',this.checked?'true':'false');};}
    // Voice-mode button visibility (#1488). localStorage-only; no server round-trip.
    // Toggling re-applies immediately via the boot.js helper so the user sees
    // the audio-waveform button appear/disappear without a reload.
    const voiceModeCb=$('settingsVoiceModeEnabled');
    if(voiceModeCb){
      voiceModeCb.checked=localStorage.getItem('hermes-voice-mode-button')==='true';
      voiceModeCb.onchange=function(){
        localStorage.setItem('hermes-voice-mode-button',this.checked?'true':'false');
        if(typeof window._applyVoiceModePref==='function') window._applyVoiceModePref();
      };
    }
    // Populate voice selector from speechSynthesis
    const ttsVoiceSel=$('settingsTtsVoice');
    if(ttsVoiceSel&&'speechSynthesis' in window){
      const populateVoices=()=>{
        const voices=speechSynthesis.getVoices();
        const current=localStorage.getItem('hermes-tts-voice')||'';
        ttsVoiceSel.innerHTML='<option value="">Default system voice</option>';
        voices.forEach(v=>{
          const opt=document.createElement('option');
          opt.value=v.name;opt.textContent=v.name+(v.lang?' ('+v.lang+')':'');
          if(v.name===current) opt.selected=true;
          ttsVoiceSel.appendChild(opt);
        });
      };
      populateVoices();
      speechSynthesis.addEventListener('voiceschanged',populateVoices,{once:true});
      ttsVoiceSel.onchange=function(){localStorage.setItem('hermes-tts-voice',this.value);};
    }
    // TTS rate/pitch sliders
    const ttsRateSlider=$('settingsTtsRate');
    const ttsRateValue=$('settingsTtsRateValue');
    if(ttsRateSlider){
      const savedRate=localStorage.getItem('hermes-tts-rate');
      ttsRateSlider.value=savedRate||'1';
      if(ttsRateValue) ttsRateValue.textContent=parseFloat(ttsRateSlider.value).toFixed(1)+'x';
      ttsRateSlider.oninput=function(){if(ttsRateValue)ttsRateValue.textContent=parseFloat(this.value).toFixed(1)+'x';localStorage.setItem('hermes-tts-rate',this.value);};
    }
    const ttsPitchSlider=$('settingsTtsPitch');
    const ttsPitchValue=$('settingsTtsPitchValue');
    if(ttsPitchSlider){
      const savedPitch=localStorage.getItem('hermes-tts-pitch');
      ttsPitchSlider.value=savedPitch||'1';
      if(ttsPitchValue) ttsPitchValue.textContent=parseFloat(ttsPitchSlider.value).toFixed(1);
      ttsPitchSlider.oninput=function(){if(ttsPitchValue)ttsPitchValue.textContent=parseFloat(this.value).toFixed(1);localStorage.setItem('hermes-tts-pitch',this.value);};
    }
    const notifCb=$('settingsNotificationsEnabled');
    if(notifCb){notifCb.checked=!!settings.notifications_enabled;notifCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    // show_thinking has no settings panel checkbox — controlled via /reasoning show|hide
    const sidebarDensitySel=$('settingsSidebarDensity');
    if(sidebarDensitySel){
      sidebarDensitySel.value=settings.sidebar_density==='detailed'?'detailed':'compact';
      sidebarDensitySel.addEventListener('change',_schedulePreferencesAutosave,{once:false});
    }
    const autoTitleRefreshSel=$('settingsAutoTitleRefresh');
    if(autoTitleRefreshSel){
      const val=String(settings.auto_title_refresh_every||'0');
      autoTitleRefreshSel.value=['0','5','10','20'].includes(val)?val:'0';
      autoTitleRefreshSel.addEventListener('change',_schedulePreferencesAutosave,{once:false});
    }
    // Busy input mode
    const busyInputModeSel=$('settingsBusyInputMode');
    if(busyInputModeSel){
      const val=String(settings.busy_input_mode||'queue');
      busyInputModeSel.value=['queue','interrupt','steer'].includes(val)?val:'queue';
      busyInputModeSel.addEventListener('change',_schedulePreferencesAutosave,{once:false});
    }
    // Bot name — debounced autosave (text input)
    const botNameField=$('settingsBotName');
    if(botNameField){
      botNameField.value=settings.bot_name||'Hermes';
      let botNameTimer=null;
      botNameField.addEventListener('input',()=>{
        if(botNameTimer) clearTimeout(botNameTimer);
        botNameTimer=setTimeout(_schedulePreferencesAutosave,500);
      },{once:false});
    }
    // Password field: always blank (we don't send hash back)
    const pwField=$('settingsPassword');
    if(pwField){pwField.value='';pwField.addEventListener('input',_markSettingsDirty,{once:false});}
    // #1560: when HERMES_WEBUI_PASSWORD env var is set, the settings password
    // field silently no-ops. Disable it + reveal the lock banner so the UI
    // tells the truth before a user tries (and the backend now also returns
    // 409 as defense-in-depth).
    const pwEnvLocked=!!settings.password_env_var;
    const pwLockBanner=$('settingsPasswordEnvLock');
    if(pwField){
      pwField.disabled=pwEnvLocked;
      if(pwEnvLocked){
        pwField.value='';
        pwField.placeholder=t('password_env_var_locked_placeholder')||pwField.placeholder;
      }
    }
    if(pwLockBanner) pwLockBanner.style.display=pwEnvLocked?'block':'none';
    // Show auth buttons only when auth is active
    try{
      const authStatus=await api('/api/auth/status');
      _setSettingsAuthButtonsVisible(!!authStatus.auth_enabled);
    }catch(e){}
    // #1560: env-var-locked password also disables the Disable Auth button —
    // clearing settings.password_hash is silent no-op when the env var is set,
    // and the backend now returns 409 anyway, so don't offer the action.
    // Sign Out remains available since it only clears the session cookie.
    if(pwEnvLocked){
      const disableBtn=$('btnDisableAuth');
      if(disableBtn) disableBtn.style.display='none';
    }
    _syncHermesPanelSessionActions();
    if(typeof loadDashboardSettings==='function') loadDashboardSettings();
    loadProvidersPanel(); // load provider cards in background
    loadPluginsPanel(); // load plugin/hook visibility in background
    switchSettingsSection(_settingsSection);
  }catch(e){
    showToast(t('settings_load_failed')+e.message);
  }
}


// ── Plugins panel (read-only plugin/hook visibility) ───────────────────────

async function loadPluginsPanel(){
  const list=$('pluginsList');
  const empty=$('pluginsEmpty');
  if(!list) return;
  try{
    const data=await api('/api/plugins');
    const plugins=Array.isArray((data||{}).plugins)?data.plugins:[];
    list.innerHTML='';
    if(plugins.length===0){
      list.style.display='none';
      if(empty) empty.style.display='';
      return;
    }
    if(empty) empty.style.display='none';
    list.style.display='';
    for(const plugin of plugins){
      list.appendChild(_buildPluginCard(plugin));
    }
  }catch(e){
    list.innerHTML='<div style="color:var(--error);padding:12px;font-size:13px">Failed to load plugins: '+esc(e.message||String(e))+'</div>';
  }
}

function _buildPluginCard(plugin){
  const card=document.createElement('div');
  card.className='provider-card plugin-card';
  card.dataset.plugin=(plugin&&plugin.key)||'';
  const hooks=Array.isArray(plugin&&plugin.hooks)?plugin.hooks:[];
  const hookHtml=hooks.length
    ? hooks.map(h=>`<span class="plugin-hook-badge">${esc(h)}</span>`).join('')
    : '<span class="plugin-hook-empty">No registered lifecycle hooks</span>';
  const version=(plugin&&plugin.version)?` · v${esc(plugin.version)}`:'';
  const desc=(plugin&&plugin.description)?esc(plugin.description):'No description provided.';
  const enabled=plugin&&plugin.enabled!==false;
  card.innerHTML=`
    <div class="provider-card-header plugin-card-header">
      <div class="provider-card-info">
        <div class="provider-card-name">${esc((plugin&&plugin.name)||'Unnamed plugin')}</div>
        <div class="provider-card-meta">${esc((plugin&&plugin.key)||'plugin')}${version}</div>
      </div>
      <span class="provider-card-badge ${enabled?'':'plugin-card-badge-disabled'}">${enabled?'Enabled':'Disabled'}</span>
    </div>
    <div class="provider-card-body plugin-card-body">
      <div class="provider-card-hint">${desc}</div>
      <div class="provider-card-label">Registered hooks</div>
      <div class="plugin-hook-list">${hookHtml}</div>
    </div>
  `;
  return card;
}

// ── Providers panel ───────────────────────────────────────────────────────

const _providerCardEls = new Map(); // providerId → {card, statusDot, input, saveBtn, removeBtn}

async function loadProvidersPanel(){
  const list=$('providersList');
  const empty=$('providersEmpty');
  if(!list) return;
  try{
    const data=await api('/api/providers');
    const quota=await api('/api/provider/quota').catch(e=>({ok:false,status:'unavailable',quota:null,message:e.message||'Quota status unavailable'}));
    const providers=(data.providers||[]).filter(p=>p.configurable||p.is_oauth);
    list.innerHTML='';
    _providerCardEls.clear();
    const quotaCard=_buildProviderQuotaCard(quota);
    if(quotaCard) list.appendChild(quotaCard);
    if(providers.length===0){
      list.style.display='none';
      if(empty) empty.style.display='';
      return;
    }
    if(empty) empty.style.display='none';
    list.style.display='';
    for(const p of providers){
      list.appendChild(_buildProviderCard(p));
    }
  }catch(e){
    list.innerHTML='<div style="color:var(--error);padding:12px;font-size:13px">Failed to load providers: '+e.message+'</div>';
  }
}

function _formatProviderQuotaMoney(value){
  if(value===null||value===undefined||value==='') return '—';
  const n=Number(value);
  if(!Number.isFinite(n)) return '—';
  return '$'+n.toFixed(2);
}

function _buildProviderQuotaCard(status){
  if(!status) return null;
  const card=document.createElement('div');
  const state=(status.status||'unavailable').replace(/[^a-z0-9_-]/gi,'').toLowerCase()||'unavailable';
  card.className='provider-quota-card provider-quota-card-'+state;
  const provider=status.display_name||status.provider||'Active provider';
  const quota=status.quota||{};
  let body='';
  if(status.status==='available'&&quota){
    body=`
      <div class="provider-quota-metric"><span>Remaining</span><strong>${esc(_formatProviderQuotaMoney(quota.limit_remaining))}</strong></div>
      <div class="provider-quota-metric"><span>Used</span><strong>${esc(_formatProviderQuotaMoney(quota.usage))}</strong></div>
      <div class="provider-quota-metric"><span>Limit</span><strong>${esc(_formatProviderQuotaMoney(quota.limit))}</strong></div>
    `;
  }else{
    body=`<div class="provider-quota-message">${esc(status.message||'Quota status unavailable')}</div>`;
  }
  card.innerHTML=`
    <div class="provider-quota-header">
      <div>
        <div class="provider-quota-title">Active provider quota</div>
        <div class="provider-quota-subtitle">${esc(provider)}</div>
      </div>
      <span class="provider-quota-badge">${esc(state.replace(/_/g,' '))}</span>
    </div>
    <div class="provider-quota-body">${body}</div>
  `;
  return card;
}

function _buildProviderCard(p){
  const card=document.createElement('div');
  card.className='provider-card';
  card.dataset.provider=p.id;
  // Use the is_oauth flag from the backend — it reflects _OAUTH_PROVIDERS in providers.py.
  // key_source can be 'oauth' (hermes auth), 'config_yaml' (token in config.yaml), or 'none'.
  const isOauth=p.is_oauth===true;
  // models_total reflects the complete catalog (e.g. 396 for a large-tier
  // Nous Portal account). The "models" array may be trimmed to a featured
  // subset for UI scannability — fall back to its length only when the
  // server didn't supply models_total (older builds, custom providers).
  const modelCount=Number.isFinite(p.models_total)
    ? p.models_total
    : (Array.isArray(p.models) ? p.models.length : 0);
  const sourceLabel=p.key_source==='oauth'
    ? t('providers_status_oauth')
    : p.key_source==='config_yaml'
      ? t('providers_status_configured')||'Configured'
      : (p.has_key ? t('providers_status_api_key') : t('providers_status_not_configured_label'));
  const metaParts=[];
  if(modelCount>0) metaParts.push(modelCount+(modelCount===1?' model':' models'));
  metaParts.push(sourceLabel);
  const metaText=metaParts.join(' · ');

  // Clickable header (toggles body)
  const header=document.createElement('button');
  header.type='button';
  header.className='provider-card-header';
  header.innerHTML=`
    <div class="provider-card-info">
      <div class="provider-card-name">${esc(p.display_name)}</div>
      <div class="provider-card-meta">${esc(metaText)}</div>
    </div>
    ${p.has_key?`<span class="provider-card-badge">${esc(t('providers_status_configured'))}</span>`:''}
    <svg class="provider-card-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" width="16" height="16"><path d="M6 9l6 6 6-6"/></svg>
  `;
  card.appendChild(header);

  const body=document.createElement('div');
  body.className='provider-card-body';

  if(isOauth){
    const hint=document.createElement('div');
    hint.className='provider-card-hint';
    if(p.key_source==='config_yaml'){
      hint.textContent=t('providers_oauth_config_yaml_hint')||'Token configured via config.yaml. To update, edit the providers section in your config.yaml or run hermes auth.';
    } else if(p.auth_error){
      hint.textContent=p.auth_error;
      hint.style.color='var(--accent)';
    } else if(p.has_key){
      hint.textContent=t('providers_oauth_hint');
    } else {
      hint.textContent=t('providers_oauth_not_configured_hint')||'Not authenticated. Run hermes auth in the terminal to configure this provider.';
      hint.style.color='var(--muted)';
    }
    body.appendChild(hint);
    card.appendChild(body);
    header.addEventListener('click',()=>card.classList.toggle('open'));
    return card;
  }

  const field=document.createElement('div');
  field.className='provider-card-field';
  const label=document.createElement('label');
  label.className='provider-card-label';
  label.textContent=t('providers_status_api_key');
  field.appendChild(label);

  const row=document.createElement('div');
  row.className='provider-card-row';
  const input=document.createElement('input');
  input.type='password';
  input.className='provider-card-input';
  input.placeholder=p.has_key?t('providers_key_placeholder_replace'):t('providers_key_placeholder_new');
  input.autocomplete='off';
  const toggleBtn=document.createElement('button');
  toggleBtn.type='button';
  toggleBtn.className='provider-card-btn provider-card-btn-ghost';
  toggleBtn.textContent='Show';
  toggleBtn.onclick=()=>{
    const revealed=input.type==='text';
    input.type=revealed?'password':'text';
    toggleBtn.textContent=revealed?'Show':'Hide';
  };
  const saveBtn=document.createElement('button');
  saveBtn.type='button';
  saveBtn.className='provider-card-btn provider-card-btn-primary';
  saveBtn.textContent=t('providers_save');
  saveBtn.onclick=()=>_saveProviderKey(p.id);
  saveBtn.disabled=true;
  row.appendChild(input);
  row.appendChild(toggleBtn);
  row.appendChild(saveBtn);
  if(p.has_key){
    const removeBtn=document.createElement('button');
    removeBtn.type='button';
    removeBtn.className='provider-card-btn provider-card-btn-danger';
    removeBtn.textContent=t('providers_remove');
    removeBtn.onclick=()=>_removeProviderKey(p.id);
    row.appendChild(removeBtn);
  }
  field.appendChild(row);
  body.appendChild(field);

  // Model list — show when provider has known models
  if(modelCount>0){
    const modelSection=document.createElement('div');
    modelSection.className='provider-card-models';
    const modelLabel=document.createElement('div');
    modelLabel.className='provider-card-label';
    modelLabel.textContent='Models';
    modelSection.appendChild(modelLabel);
    const modelList=document.createElement('div');
    modelList.className='provider-card-model-tags';
    const renderedModels=Array.isArray(p.models)?p.models:[];
    for(const m of renderedModels){
      const tag=document.createElement('span');
      tag.className='provider-card-model-tag';
      tag.textContent=m.id||m.label||m;
      modelList.appendChild(tag);
    }
    // When the rendered list is a strict subset of the total catalog (Nous
    // Portal large-tier accounts hit this with ~400-model catalogs), show
    // a "+N more" trailing pill so the user knows the picker is intentionally
    // capped — and they can still reach the full catalog via the /model
    // slash command (its autocomplete consumes the un-trimmed list from
    // /api/models's extra_models field). #1567.
    const totalCount=Number.isFinite(p.models_total)?p.models_total:renderedModels.length;
    const hiddenCount=Math.max(0, totalCount - renderedModels.length);
    if(hiddenCount>0){
      const more=document.createElement('span');
      more.className='provider-card-model-tag provider-card-model-tag-more';
      more.textContent='+'+hiddenCount+' more';
      more.title='The /model slash command can autocomplete every model in this provider\'s catalog.';
      modelList.appendChild(more);
    }
    modelSection.appendChild(modelList);
    body.appendChild(modelSection);
  }

  // Refresh models for this provider
  const refreshRow=document.createElement('div');
  refreshRow.className='provider-card-row';
  refreshRow.style.marginTop='6px';
  const refreshBtn=document.createElement('button');
  refreshBtn.type='button';
  refreshBtn.className='provider-card-btn provider-card-btn-ghost';
  refreshBtn.style.display='flex';
  refreshBtn.style.alignItems='center';
  refreshBtn.style.gap='5px';
  refreshBtn.innerHTML=`<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M3 21v-5h5"/></svg> ${t('providers_refresh_models')||'Refresh Models'}`;
  refreshBtn.onclick=()=>_refreshProviderModels(p.id, refreshBtn);
  refreshRow.appendChild(refreshBtn);
  body.appendChild(refreshRow);
  card.appendChild(body);

  _providerCardEls.set(p.id,{card,input,saveBtn,hasKey:p.has_key});
  input.addEventListener('input',()=>{saveBtn.disabled=!input.value.trim();});
  header.addEventListener('click',e=>{
    // Don't toggle when clicking inside body (defensive; body isn't inside header)
    if(e.target.closest('.provider-card-body')) return;
    card.classList.toggle('open');
    if(card.classList.contains('open')) setTimeout(()=>input.focus(),0);
  });
  return card;
}

async function _saveProviderKey(providerId){
  const els=_providerCardEls.get(providerId);
  if(!els) return;
  const key=els.input.value.trim();
  if(!key){
    showToast(t('providers_enter_key'));
    return;
  }
  els.saveBtn.disabled=true;
  els.saveBtn.textContent=t('providers_saving');
  try{
    const res=await api('/api/providers',{method:'POST',body:JSON.stringify({provider:providerId,api_key:key})});
    if(res.ok){
      showToast(res.provider+' key '+res.action);
      els.input.value='';
      // Invalidate every dropdown surface that caches /api/models so the
      // newly-configured provider's models show up without a server restart
      // or page reload (#1539). Server-side invalidate_models_cache() is
      // already called by api/providers.py:set_provider_key.
      _refreshModelDropdownsAfterProviderChange();
      await loadProvidersPanel(); // refresh list
    }else{
      showToast(res.error||'Failed to save key');
      els.saveBtn.disabled=false;
      els.saveBtn.textContent=t('providers_save');
    }
  }catch(e){
    showToast('Error: '+e.message);
    els.saveBtn.disabled=false;
    els.saveBtn.textContent=t('providers_save');
  }
}

async function _removeProviderKey(providerId){
  const els=_providerCardEls.get(providerId);
  if(!els) return;
  if(els.saveBtn){els.saveBtn.disabled=true;els.saveBtn.textContent=t('providers_removing');}
  try{
    const res=await api('/api/providers/delete',{method:'POST',body:JSON.stringify({provider:providerId})});
    if(res.ok){
      showToast(res.provider+' key '+t('providers_key_removed').toLowerCase());
      // Drop the removed provider from every cached dropdown surface so it
      // disappears immediately — composer picker, /model slash command,
      // Settings → Default Model, configured-model badges (#1539).
      // Without this, a stale list from before the delete keeps offering
      // the now-removed provider's models until the page is reloaded.
      _refreshModelDropdownsAfterProviderChange();
      await loadProvidersPanel(); // refresh list
    }else{
      showToast(res.error||'Failed to remove key');
      if(els.saveBtn){els.saveBtn.disabled=false;els.saveBtn.textContent=t('providers_save');}
    }
  }catch(e){
    showToast('Error: '+e.message);
    if(els.saveBtn){els.saveBtn.disabled=false;els.saveBtn.textContent=t('providers_save');}
  }
}

// Shared dropdown-cache flush invoked after a provider add/remove. The
// server-side TTL cache is already invalidated by /api/providers and
// /api/providers/delete (via api/providers.py:set_provider_key); this
// flushes the JS-side caches so the next render rebuilds from a fresh
// /api/models response. Wrapped in a try/catch so a UI module that hasn't
// loaded yet (e.g. during early Settings open) cannot break the save flow.
function _refreshModelDropdownsAfterProviderChange(){
  try{
    if(typeof window._invalidateSlashModelCache==='function'){
      window._invalidateSlashModelCache();
    }
    if(typeof populateModelDropdown==='function'){
      // Fire-and-forget: don't block the providers panel refresh on a
      // dropdown rebuild. The composer/Settings dropdowns will catch up
      // on the very next paint frame.
      Promise.resolve(populateModelDropdown()).catch(()=>{});
    }
  }catch(_e){
    // Swallow — dropdown refresh is best-effort, providers panel must still update.
  }
}

async function _refreshProviderModels(providerId, btn){
  btn.disabled=true;
  const orig=btn.innerHTML;
  btn.innerHTML=`<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M3 21v-5h5"/></svg> ${t('providers_refreshing')||'Refreshing...'}`;
  try{
    const res=await api('/api/models/refresh',{method:'POST',body:JSON.stringify({provider:providerId})});
    if(res.ok){
      showToast(t('providers_models_refreshed')||('Models refreshed for '+res.provider));
    }else{
      showToast(res.error||'Failed to refresh models');
    }
  }catch(e){
    showToast('Error: '+e.message);
  }finally{
    btn.disabled=false;
    btn.innerHTML=orig;
  }
}

function _setSettingsAuthButtonsVisible(active){
  const signOutBtn=$('btnSignOut');
  if(signOutBtn) signOutBtn.style.display=active?'':'none';
  const disableBtn=$('btnDisableAuth');
  if(disableBtn) disableBtn.style.display=active?'':'none';
}

function _applySavedSettingsUi(saved, body, opts){
  const {sendKey,showTokenUsage,showTps,showCliSessions,theme,skin,language,sidebarDensity,fontSize}=opts;
  window._sendKey=sendKey||'enter';
  window._showTokenUsage=showTokenUsage;
  window._showTps=showTps;
  window._showCliSessions=showCliSessions;
  window._soundEnabled=body.sound_enabled;
  window._notificationsEnabled=body.notifications_enabled;
  window._showThinking=body.show_thinking!==false;
  window._simplifiedToolCalling=body.simplified_tool_calling!==false;
  window._sidebarDensity=sidebarDensity==='detailed'?'detailed':'compact';
  window._busyInputMode=body.busy_input_mode||'queue';
  window._botName=body.bot_name||'Hermes';
  if(typeof applyBotName==='function') applyBotName();
  if(typeof setLocale==='function') setLocale(language);
  if(typeof applyLocaleToDOM==='function') applyLocaleToDOM();
  if(typeof startGatewaySSE==='function'){
    if(showCliSessions) startGatewaySSE();
    else if(typeof stopGatewaySSE==='function') stopGatewaySSE();
  }
  _setSettingsAuthButtonsVisible(!!saved.auth_enabled);
  _settingsDirty=false;
  _settingsThemeOnOpen=theme;
  _settingsSkinOnOpen=skin||'default';
  _settingsFontSizeOnOpen=fontSize||localStorage.getItem('hermes-font-size')||'default';
  const bar=$('settingsUnsavedBar');
  if(bar) bar.style.display='none';
  _settingsHermesDefaultModelOnOpen=body.default_model||_settingsHermesDefaultModelOnOpen||'';
  // Sync window._defaultModel so newSession() uses the just-saved default without a reload (#908).
  if(body.default_model) window._defaultModel=body.default_model;
  if(typeof clearMessageRenderCache==='function') clearMessageRenderCache();
  renderMessages();
  if(typeof syncTopbar==='function') syncTopbar();
  if(typeof renderSessionList==='function') renderSessionList();
}

async function checkUpdatesNow(){
  const btn=$('btnCheckUpdatesNow');
  const label=$('checkUpdatesLabel');
  const spinner=$('checkUpdatesSpinner');
  const status=$('checkUpdatesStatus');
  if(!btn||!label) return;
  // Disable button, show spinner
  btn.disabled=true;
  if(spinner) spinner.style.display='';
  if(label) label.textContent=t('settings_checking');
  if(status) status.textContent='';
  try {
    const data=await api('/api/updates/check?force=1');
    if(data.disabled){
      if(status){status.textContent=t('settings_updates_disabled');status.style.color='var(--muted)';}
    } else {
      const parts=[];
      const formatUpdatePart=(typeof _formatUpdateTargetStatus==='function')
        ? _formatUpdateTargetStatus
        : ((label,info)=>info&&info.behind>0?label+': '+info.behind:null);
      const webuiPart=formatUpdatePart('WebUI',data.webui);
      const agentPart=formatUpdatePart('Agent',data.agent);
      if(webuiPart) parts.push(webuiPart);
      if(agentPart) parts.push(agentPart);
      if(parts.length){
        if(status){status.textContent=t('settings_updates_available').replace('{count}',parts.join(', '));status.style.color='var(--accent)';}
        // Also trigger the update banner
        if(typeof _showUpdateBanner==='function') _showUpdateBanner(data);
      } else {
        if(status){status.textContent=t('settings_up_to_date');status.style.color='var(--success)';}
      }
    }
  } catch(e){
    // Never expose raw e.message in UI — log to console for debugging only
    console.warn('[checkUpdatesNow]', e);
    // Show a generic user-facing error; if the API returned a message body use it
    let userMsg=t('settings_update_check_failed');
    if(e&&e.response){
      try{
        const body=JSON.parse(e.response);
        if(body.error) userMsg=String(body.error).substring(0,120);
      }catch(_){}
    }
    if(status){status.textContent=userMsg;status.style.color='var(--error)';}
  } finally {
    btn.disabled=false;
    if(spinner) spinner.style.display='none';
    if(label) label.textContent=t('settings_check_now');
  }
}

async function saveSettings(andClose){
  const model=($('settingsModel')||{}).value;
  const modelChanged=(model||'')!==(_settingsHermesDefaultModelOnOpen||'');
  const sendKey=($('settingsSendKey')||{}).value;
  const showTokenUsage=!!($('settingsShowTokenUsage')||{}).checked;
  const showTps=!!($('settingsShowTps')||{}).checked;
  const showCliSessions=!!($('settingsShowCliSessions')||{}).checked;
  const pw=($('settingsPassword')||{}).value;
  const theme=($('settingsTheme')||{}).value||'dark';
  const skin=($('settingsSkin')||{}).value||'default';
  const fontSize=($('settingsFontSize')||{}).value||localStorage.getItem('hermes-font-size')||'default';
  const language=($('settingsLanguage')||{}).value||'en';
  const sidebarDensity=($('settingsSidebarDensity')||{}).value==='detailed'?'detailed':'compact';
  const busyInputMode=($('settingsBusyInputMode')||{}).value||'queue';
  const body={};

  if(sendKey) body.send_key=sendKey;
  body.theme=theme;
  body.skin=skin;
  body.font_size=fontSize;
  body.language=language;
  body.show_token_usage=showTokenUsage;
  body.show_tps=showTps;
  body.simplified_tool_calling=!!($('settingsSimplifiedToolCalling')||{}).checked;
  body.api_redact_enabled=!!($('settingsApiRedact')||{}).checked;
  body.show_cli_sessions=showCliSessions;
  body.sync_to_insights=!!($('settingsSyncInsights')||{}).checked;
  body.check_for_updates=!!($('settingsCheckUpdates')||{}).checked;
  body.sound_enabled=!!($('settingsSoundEnabled')||{}).checked;
  body.notifications_enabled=!!($('settingsNotificationsEnabled')||{}).checked;
  body.show_thinking=window._showThinking!==false;
  body.sidebar_density=sidebarDensity;
  body.busy_input_mode=busyInputMode;
  body.auto_title_refresh_every=(($('settingsAutoTitleRefresh')||{}).value||'0');
  const botName=(($('settingsBotName')||{}).value||'').trim();
  body.bot_name=botName||'Hermes';
  // Password: only act if the field has content; blank = leave auth unchanged
  if(pw && pw.trim()){
    try{
      const saved=await api('/api/settings',{method:'POST',body:JSON.stringify({...body,_set_password:pw.trim()})});
      if(modelChanged && model){
        try{
          await api('/api/default-model',{method:'POST',body:JSON.stringify({model})});
          body.default_model=model;
        }catch(_modelErr){
          if(typeof showToast==='function') showToast('Failed to update default model — settings saved');
        }
      }
      _applySavedSettingsUi(saved, body, {sendKey,showTokenUsage,showTps,showCliSessions,theme,skin,language,sidebarDensity,fontSize});
      showToast(t(saved.auth_just_enabled?'settings_saved_pw':'settings_saved_pw_updated'));
      _settingsDirty=false;
      _resetSettingsPanelState();
      if(!andClose) _pendingSettingsTargetPanel = null;
      if(andClose) _hideSettingsPanel();
      return;
    }catch(e){showToast(t('settings_save_failed')+e.message);return;}
  }
  try{
    const saved=await api('/api/settings',{method:'POST',body:JSON.stringify(body)});
    if(modelChanged && model){
      try{
        await api('/api/default-model',{method:'POST',body:JSON.stringify({model})});
        body.default_model=model;
      }catch(_modelErr){
        if(typeof showToast==='function') showToast('Failed to update default model — settings saved');
      }
    }
    _applySavedSettingsUi(saved, body, {sendKey,showTokenUsage,showTps,showCliSessions,theme,skin,language,sidebarDensity,fontSize});
    showToast(t('settings_saved'));
    _settingsDirty=false;
    _resetSettingsPanelState();
    if(!andClose) _pendingSettingsTargetPanel = null;
    if(andClose) _hideSettingsPanel();
  }catch(e){
    showToast(t('settings_save_failed')+e.message);
  }
}

async function signOut(){
  try{
    await api('/api/auth/logout',{method:'POST',body:'{}'});
    window.location.href='login';
  }catch(e){
    showToast(t('sign_out_failed')+e.message);
  }
}

async function disableAuth(){
  const _disAuth=await showConfirmDialog({title:t('disable_auth_confirm_title'),message:t('disable_auth_confirm_message'),confirmLabel:t('disable'),danger:true,focusCancel:true});
  if(!_disAuth) return;
  try{
    await api('/api/settings',{method:'POST',body:JSON.stringify({_clear_password:true})});
    showToast(t('auth_disabled'));
    // Hide both auth buttons since auth is now off
    const disableBtn=$('btnDisableAuth');
    if(disableBtn) disableBtn.style.display='none';
    const signOutBtn=$('btnSignOut');
    if(signOutBtn) signOutBtn.style.display='none';
  }catch(e){
    showToast(t('disable_auth_failed')+e.message);
  }
}


// ── Cron completion alerts ────────────────────────────────────────────────────

let _cronPollSince=Date.now()/1000;  // track from page load
let _cronPollTimer=null;
let _cronUnreadCount=0;
const _cronNewJobIds=new Set();  // track which job IDs had new completions (unread)

// Auto-refresh the cron list when a job is created from chat or any external source.
// The chat path dispatches this event when the agent response mentions cron creation.
window.addEventListener('hermes:cron_created', () => {
  if ($('cronList')) loadCrons();
});

function startCronPolling(){
  if(_cronPollTimer) return;
  _cronPollTimer=setInterval(async()=>{
    if(document.hidden) return;  // don't poll when tab is in background
    try{
      const data=await api(`/api/crons/recent?since=${_cronPollSince}`);
      if(data.completions&&data.completions.length>0){
        for(const c of data.completions){
          showToast(t('cron_completion_status', c.name, c.status==='error' ? t('status_failed') : t('status_completed')),4000);
          _cronPollSince=Math.max(_cronPollSince,c.completed_at);
          if(c.job_id) _cronNewJobIds.add(String(c.job_id));
        }
        // _cronUnreadCount is derived from _cronNewJobIds.size in updateCronBadge.
        updateCronBadge();
      }
    }catch(e){}
  },30000);
}

function updateCronBadge(){
  const tab=document.querySelector('.nav-tab[data-panel="tasks"]');
  if(!tab) return;
  let badge=tab.querySelector('.cron-badge');
  _cronUnreadCount=_cronNewJobIds.size;  // sync counter to set (source of truth)
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

// Clear cron badge only when all unread jobs have been viewed (not on panel open)
function _clearCronUnreadForJob(jobId){
  const id=String(jobId);
  if(_cronNewJobIds.has(id)){
    _cronNewJobIds.delete(id);
    updateCronBadge();  // re-derives _cronUnreadCount from set size
  }
}

const _origSwitchPanel=switchPanel;
switchPanel=async function(name){ return _origSwitchPanel(name); };

// Start polling on page load
startCronPolling();

// ── Background agent error tracking ──────────────────────────────────────────

const _backgroundErrors=[];  // {session_id, title, message, ts}

function trackBackgroundError(sessionId, title, message){
  // Only track if user is NOT currently viewing this session
  if(S.session&&S.session.session_id===sessionId) return;
  _backgroundErrors.push({session_id:sessionId, title:title||t('untitled'), message, ts:Date.now()});
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
  const msg=count>1?t('bg_error_multi',count):t('bg_error_single',latest.title);
  banner.innerHTML=`<span>\u26a0 ${esc(msg)}</span><div style="display:flex;gap:6px;flex-shrink:0"><button class="reconnect-btn" onclick="navigateToErrorSession()">${esc(t('view'))}</button><button class="reconnect-btn" onclick="dismissErrorBanner()">${esc(t('dismiss'))}</button></div>`;
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


// ── MCP Server Management ──
function _mcpStatusLabel(status){
  const key={
    active:'mcp_status_active',
    configured:'mcp_status_configured',
    disabled:'mcp_status_disabled',
    invalid_config:'mcp_status_invalid_config',
  }[status]||'mcp_status_unknown';
  return t(key);
}
function loadMcpServers(){
  const list=$('mcpServerList');
  if(!list) return;
  list.innerHTML=`<div style="color:var(--muted);font-size:12px;padding:6px 0">${esc(t('loading'))}</div>`;
  api('/api/mcp/servers').then(r=>{
    if(!r||!Array.isArray(r.servers)) return;
    if(!r.servers.length){
      list.innerHTML=`<div class="mcp-empty-state" style="color:var(--muted);font-size:12px;padding:6px 0">${esc(t('mcp_no_servers'))}</div>`;
      return;
    }
    const toggleNote=r.toggle_supported?'':'<div class="mcp-readonly-note">'+esc(t('mcp_toggle_followup'))+'</div>';
    list.innerHTML=r.servers.map(s=>{
      const transportLabel=s.transport==='http'?'HTTP':s.transport==='stdio'?'stdio':(''+(s.transport||'unknown'));
      const transportClass=s.transport==='http'?'mcp-http':s.transport==='stdio'?'mcp-stdio':'mcp-unknown';
      const transportBadge=`<span class="mcp-transport-badge ${transportClass}">${esc(transportLabel)}</span>`;
      const status=s.status||'configured';
      const statusBadge=`<span class="mcp-status-badge mcp-status-${esc(status)}">${esc(_mcpStatusLabel(status))}</span>`;
      const toolCount=s.tool_count===null||typeof s.tool_count==='undefined'?'—':String(s.tool_count);
      const detail=s.transport==='http'
        ? (s.url||'')
        : (s.transport==='stdio'?`${s.command||''} ${Array.isArray(s.args)?s.args.join(' '):''}`:t('mcp_status_invalid_config'));
      const envInfo=s.env?Object.entries(s.env).map(([k,v])=>`${k}=${v}`).join(', '):'';
      const headersInfo=s.headers?Object.entries(s.headers).map(([k,v])=>`${k}=${v}`).join(', '):'';
      const secretInfo=[envInfo,headersInfo].filter(Boolean).join(' | ');
      return `<div class="mcp-server-row">
        <div class="mcp-server-row-head">
          <span class="mcp-server-name">${esc(s.name)}</span>
          ${transportBadge}
          ${statusBadge}
        </div>
        <div class="mcp-server-detail">${esc(detail)}${secretInfo?' | '+esc(secretInfo):''}</div>
        <div class="mcp-server-meta"><span class="mcp-tool-count">${esc(t('mcp_tool_count',toolCount))}</span><span>${esc(t(s.enabled===false?'mcp_enabled_no':'mcp_enabled_yes'))}</span></div>
      </div>`;
    }).join('')+toggleNote;
  }).catch(()=>{list.innerHTML=`<div class="mcp-error-state" style="color:#ef4444;font-size:12px;padding:6px 0">${esc(t('mcp_load_failed'))}</div>`});
}
let _mcpToolsCache=[];
function _filterMcpToolsForSearch(tools, query){
  const q=(query||'').trim().toLowerCase();
  if(!q) return Array.isArray(tools)?tools:[];
  return (Array.isArray(tools)?tools:[]).filter(tool=>{
    const hay=[tool.name,tool.server,tool.description].map(v=>String(v||'').toLowerCase()).join(' ');
    return hay.includes(q);
  });
}
function _mcpToolSchemaText(schemaSummary){
  if(!Array.isArray(schemaSummary)||!schemaSummary.length) return t('mcp_tools_schema_empty');
  return schemaSummary.map(p=>{
    const req=p.required?'*':'';
    const desc=p.description?` — ${p.description}`:'';
    return `${p.name}${req}: ${p.type||'unknown'}${desc}`;
  }).join('\n');
}
function _renderMcpTools(tools, query){
  const list=$('mcpToolList');
  if(!list) return;
  const filtered=_filterMcpToolsForSearch(tools, query);
  if(!filtered.length){
    const key=query?'mcp_tools_no_matches':'mcp_tools_no_tools';
    list.innerHTML=`<div class="mcp-tool-empty-state" style="color:var(--muted);font-size:12px;padding:6px 0">${esc(t(key))}</div>`;
    return;
  }
  list.innerHTML=filtered.map(tool=>{
    const status=tool.status||'unknown';
    const statusBadge=`<span class="mcp-status-badge mcp-status-${esc(status)}">${esc(_mcpStatusLabel(status))}</span>`;
    const schemaText=_mcpToolSchemaText(tool.schema_summary);
    return `<div class="mcp-tool-row">
      <div class="mcp-server-row-head">
        <span class="mcp-tool-name">${esc(tool.name)}</span>
        <span class="mcp-tool-server">${esc(tool.server||'unknown')}</span>
        ${statusBadge}
      </div>
      <div class="mcp-server-detail">${esc(tool.description||'')}</div>
      <pre class="mcp-tool-schema">${esc(schemaText)}</pre>
    </div>`;
  }).join('');
}
function filterMcpTools(){
  const input=$('mcpToolSearch');
  _renderMcpTools(_mcpToolsCache,input?input.value:'');
}
function loadMcpTools(){
  const list=$('mcpToolList');
  if(!list) return;
  list.innerHTML=`<div style="color:var(--muted);font-size:12px;padding:6px 0">${esc(t('loading'))}</div>`;
  api('/api/mcp/tools').then(r=>{
    _mcpToolsCache=(r&&Array.isArray(r.tools))?r.tools:[];
    filterMcpTools();
  }).catch(()=>{list.innerHTML=`<div class="mcp-tool-error-state" style="color:#ef4444;font-size:12px;padding:6px 0">${esc(t('mcp_tools_load_failed'))}</div>`});
}
function loadGatewayStatus(){
  const card=$('gatewayStatusCard');
  if(!card) return;
  api('/api/gateway/status').then(r=>{
    if(!r) return;
    if(!r.running){
      card.innerHTML=`<div style="color:var(--muted);font-size:12px;display:flex;align-items:center;gap:6px"><span style="width:8px;height:8px;border-radius:50%;background:#ef4444;display:inline-block"></span>Gateway not running</div>`;
      return;
    }
    const platformIcons={telegram:'💬',discord:'🎮',slack:'📝',web:'🌐',api:'🔌'};
    let badges='';
    if(r.platforms&&r.platforms.length){
      badges=r.platforms.map(p=>{
        const icon=platformIcons[p.name]||'📡';
        return `<span style="display:inline-flex;align-items:center;gap:4px;padding:3px 10px;background:var(--code-bg);border:1px solid var(--border2);border-radius:12px;font-size:12px;font-weight:500">${icon} ${esc(p.label)}</span>`;
      }).join(' ');
    }
    const lastActive=r.last_active?`<span style="font-size:11px;color:var(--muted)">Last active: ${esc(new Date(r.last_active).toLocaleString())}</span>`:'';
    const sessionInfo=r.session_count?`<span style="font-size:11px;color:var(--muted)">${r.session_count} session${r.session_count!==1?'s':''}</span>`:'';
    card.innerHTML=`<div style="display:flex;align-items:center;gap:6px;margin-bottom:8px"><span style="width:8px;height:8px;border-radius:50%;background:#22c55e;display:inline-block"></span><span style="font-size:13px;font-weight:500;color:#22c55e">Running</span></div>${badges?`<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">${badges}</div>`:''}<div style="display:flex;gap:12px">${sessionInfo}${lastActive}</div>`;
  }).catch(()=>{card.innerHTML=`<div style="color:#ef4444;font-size:12px">Failed to load gateway status</div>`});
}
// Load MCP servers when system settings tab opens
const _origSwitchSettings=switchSettingsSection;
switchSettingsSection=function(name){
  _origSwitchSettings(name);
  if(name==='system'){loadMcpServers();loadMcpTools();loadGatewayStatus();}
};

// ── Checkpoints / Rollback ──────────────────────────────────────────────────

async function _loadCheckpoints(workspace){
  const container=$('checkpointListContainer');
  if(!container) return;
  try{
    const data=await api(`/api/rollback/list?workspace=${encodeURIComponent(workspace)}`);
    const checkpoints=data.checkpoints||[];
    if(!checkpoints.length){
      container.innerHTML=`<div style="color:var(--muted);font-size:12px;padding:8px 0">${esc(t('checkpoint_empty'))}</div>`;
      return;
    }
    let html='';
    for(const ck of checkpoints){
      const shortId=ck.id||ck.commit||'?';
      const msg=ck.message||'checkpoint';
      const date=ck.date_display||ck.date||'';
      const files=ck.files||0;
      html+=`
        <div class="detail-row" style="align-items:center;padding:6px 0;border-bottom:1px solid var(--border,rgba(255,255,255,0.08))">
          <div style="flex:1;min-width:0">
            <div style="font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(msg)}">${esc(msg)}</div>
            <div style="font-size:11px;color:var(--muted);margin-top:2px">
              <code style="font-size:10px">${esc(shortId)}</code>
              ${date ? ` · ${esc(date)}` : ''}
              ${files ? ` · ${esc(t('checkpoint_files'))}: ${files}` : ''}
            </div>
          </div>
          <div style="display:flex;gap:4px;flex-shrink:0;margin-left:8px">
            <button class="panel-head-btn" title="${esc(t('checkpoint_view_diff'))}" onclick="event.stopPropagation();_viewCheckpointDiff('${esc(workspace)}','${esc(ck.id)}')">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="16" height="16"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
            </button>
            <button class="panel-head-btn" title="${esc(t('checkpoint_restore'))}" onclick="event.stopPropagation();_restoreCheckpoint('${esc(workspace)}','${esc(ck.id)}','${esc(msg.replace(/'/g,"\\'"))}')">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="16" height="16"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
            </button>
          </div>
        </div>`;
    }
    container.innerHTML=html;
  }catch(e){
    container.innerHTML=`<div style="color:var(--error,#f87171);font-size:12px;padding:8px 0">${esc(t('checkpoint_error'))}: ${esc(e.message)}</div>`;
  }
}

async function _viewCheckpointDiff(workspace,checkpoint){
  const modal=document.getElementById('checkpointDiffModal');
  if(!modal){
    const m=document.createElement('div');
    m.id='checkpointDiffModal';
    m.style.cssText='position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.6)';
    m.innerHTML=`
      <div style="background:var(--bg,${getComputedStyle(document.documentElement).getPropertyValue('--bg')||'#1a1a2e'});border:1px solid var(--border,rgba(255,255,255,0.12));border-radius:12px;width:90vw;max-width:800px;max-height:80vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.4)">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border,rgba(255,255,255,0.08))">
          <div id="checkpointDiffModalTitle" style="font-weight:600;font-size:14px"></div>
          <button onclick="document.getElementById('checkpointDiffModal').style.display='none'" style="background:none;border:none;color:var(--fg);cursor:pointer;font-size:18px;padding:0 4px">&times;</button>
        </div>
        <div id="checkpointDiffModalBody" style="flex:1;overflow:auto;padding:12px 16px">
          <div style="color:var(--muted);font-size:12px">${esc(t('checkpoint_loading'))}</div>
        </div>
      </div>`;
    m.onclick=(e)=>{if(e.target===m) m.style.display='none';};
    document.body.appendChild(m);
  }
  modal.style.display='flex';
  $('checkpointDiffModalTitle').textContent=t('checkpoint_diff_title');
  $('checkpointDiffModalBody').innerHTML=`<div style="color:var(--muted);font-size:12px">${esc(t('checkpoint_loading'))}</div>`;
  try{
    const data=await api(`/api/rollback/diff?workspace=${encodeURIComponent(workspace)}&checkpoint=${encodeURIComponent(checkpoint)}`);
    const body=$('checkpointDiffModalBody');
    if(!data.total_changes){
      body.innerHTML=`<div style="color:var(--muted);font-size:12px">${esc(t('checkpoint_diff_no_changes'))}</div>`;
      return;
    }
    let html=`<div style="font-size:12px;margin-bottom:8px">${esc(t('checkpoint_diff_files_changed',data.total_changes))}</div>`;
    if(data.files_changed){
      html+='<div style="margin-bottom:8px">';
      for(const f of data.files_changed){
        const icon=f.status==='deleted'?'−':'~';
        const color=f.status==='deleted'?'var(--error,#f87171)':'var(--accent,#60a5fa)';
        html+=`<div style="font-size:12px;padding:2px 0"><span style="color:${color};font-weight:bold;margin-right:6px">${icon}</span><code style="font-size:11px">${esc(f.file)}</code></div>`;
      }
      html+='</div>';
    }
    if(data.diff){
      html+=`<pre style="background:var(--bg-secondary,rgba(0,0,0,0.3));border:1px solid var(--border,rgba(255,255,255,0.08));border-radius:8px;padding:12px;font-size:11px;line-height:1.4;overflow-x:auto;white-space:pre-wrap;word-break:break-all;max-height:50vh;overflow-y:auto;color:var(--fg)">${esc(data.diff)}</pre>`;
    }
    body.innerHTML=html;
  }catch(e){
    $('checkpointDiffModalBody').innerHTML=`<div style="color:var(--error,#f87171);font-size:12px">${esc(e.message)}</div>`;
  }
}

async function _restoreCheckpoint(workspace,checkpoint,message){
  const label=message||checkpoint;
  const ok=await showConfirmDialog({title:t('checkpoint_restore_confirm_title'),message:t('checkpoint_restore_confirm_message',label),confirmLabel:t('checkpoint_restore'),danger:true,focusCancel:true});
  if(!ok) return;
  try{
    const data=await api('/api/rollback/restore',{method:'POST',body:JSON.stringify({workspace,checkpoint})});
    if(data&&data.ok){
      showToast(t('checkpoint_restored')+(data.files_restored_count?` (${data.files_restored_count} ${t('checkpoint_files').toLowerCase()})`:''));
    }else{
      showToast((data&&data.error)||'Restore failed','error');
    }
  }catch(e){
    showToast(t('checkpoint_restore')+': '+e.message,'error');
  }
}
