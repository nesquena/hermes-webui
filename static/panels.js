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
let _kanbanIsDispatching = false;
let _kanbanSuppressCardClickUntil = 0;
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
let _logsSeverityFilter = 'all';

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
  _settingsAvatarPresenceLayoutOnOpen = localStorage.getItem('hermes-avatar-presence-layout') || 'thread';
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

function _resyncChatSidebarAfterPanelSwitch() {
  if (_currentPanel !== 'chat') return;
  if (typeof renderSessionListFromCache !== 'function') return;
  const run = () => {
    if (_currentPanel !== 'chat') return;
    if (typeof _renamingSid !== 'undefined' && _renamingSid) return;
    // If the user opens the per-conversation action menu immediately after
    // returning to Chat, do not let the deferred sidebar resync tear it down.
    // renderSessionListFromCache() intentionally closes that menu before it
    // rebuilds rows, which is correct for normal list refreshes but hostile to
    // this one-shot panel-transition repair.
    if (typeof _sessionActionMenu !== 'undefined' && _sessionActionMenu) return;
    renderSessionListFromCache();
  };
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run);
  else run();
}

async function switchPanel(name, opts = {}) {
  const nextPanel = name || 'chat';
  const prevPanel = _currentPanel;
  // ── Desktop sidebar collapse toggle (rail-click only) ──
  // If the click came from a rail icon AND we're on desktop, the rail icon
  // does double duty: clicking the already-active panel collapses the sidebar;
  // clicking any panel while collapsed expands first. Programmatic switches
  // (no opts.fromRailClick) are unaffected so legacy callers preserve
  // behaviour exactly.
  if (opts.fromRailClick && typeof _isSidebarCollapsed === 'function'
      && typeof _isDesktopWidth === 'function' && _isDesktopWidth()) {
    if (_isSidebarCollapsed()) {
      // Expand first, then continue to the normal panel switch below so
      // the clicked panel becomes (or stays) active in the same gesture.
      expandSidebar();
    } else if (prevPanel === nextPanel) {
      // Same panel clicked while sidebar is open → collapse and short-circuit.
      // Skip the guard/cleanup work below; nothing about the active panel
      // is changing, only the visibility of the panel container.
      toggleSidebar(true);
      return false;
    }
  }
  if (!opts.bypassSettingsGuard && !_beforePanelSwitch(nextPanel)) return false;
  if (prevPanel !== 'settings' && nextPanel === 'settings') _beginSettingsPanelSession();
  // Close any long-lived Kanban SSE stream when leaving the kanban panel
  // so we don't keep a stale connection open in the background.
  if (prevPanel === 'kanban' && nextPanel !== 'kanban') {
    if (typeof _kanbanStopPolling === 'function') _kanbanStopPolling();
  }
  // Stop the 1500ms gateway status interval when leaving the Profiles panel
  // so we don't leak pollers across panel switches.
  if (prevPanel === 'profiles' && nextPanel !== 'profiles') {
    if (typeof _stopAllGatewayPollers === 'function') _stopAllGatewayPollers();
  }
  _currentPanel = nextPanel;
  // Update nav tabs (rail + mobile sidebar-nav share data-panel)
  document.querySelectorAll('[data-panel]').forEach(t => t.classList.toggle('active', t.dataset.panel === nextPanel));
  // Refresh aria-expanded on the newly-active rail button to mirror sidebar state.
  if (typeof _syncSidebarAria === 'function') _syncSidebarAria();
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
  if (nextPanel === 'chat' && typeof scheduleComposerPresenceAvatarMeasureSettled === 'function') {
    scheduleComposerPresenceAvatarMeasureSettled();
  } else if (nextPanel === 'chat' && typeof scheduleComposerPresenceAvatarMeasure === 'function') {
    scheduleComposerPresenceAvatarMeasure();
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
  if (opts.fromRailClick && typeof _isDesktopWidth === 'function' && !_isDesktopWidth()) {
    const sidebar = document.querySelector('.sidebar');
    const overlay = document.getElementById('mobileOverlay');
    if (sidebar) sidebar.classList.add('mobile-open');
    if (overlay) overlay.classList.add('visible');
  }
  _resyncChatSidebarAfterPanelSwitch();
  syncAppTitlebar();
  return true;
}

// ── Cron panel ──
function _isRecurringCronJob(job) {
  const kind = job && job.schedule && job.schedule.kind;
  return kind === 'cron' || kind === 'interval';
}

function _cronScheduleKindForInput(value) {
  const schedule = String(value || '').trim();
  if (!schedule) return '';
  const lower = schedule.toLowerCase();
  if (lower.startsWith('every ')) return 'interval';
  if (lower.startsWith('@')) return 'cron';
  const parts = schedule.split(/\s+/);
  if (parts.length >= 5 && parts.slice(0, 5).every(p => /^[\d*\-,/]+$/.test(p))) return 'cron';
  if (schedule.includes('T') || /^\d{4}-\d{2}-\d{2}/.test(schedule)) return 'once';
  if (/^\d+\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$/i.test(schedule)) return 'once';
  return '';
}

function _syncCronScheduleWarning() {
  const input = $('cronFormSchedule');
  const warning = $('cronFormScheduleOnceWarning');
  if (!input || !warning) return;
  warning.style.display = _cronScheduleKindForInput(input.value) === 'once' ? '' : 'none';
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

function _cronGatewayNoticeHtml(status) {
  if (!status || (status.configured && status.running)) return '';
  const notConfigured = !status.configured;
  const title = notConfigured
    ? 'Gateway not configured'
    : 'Gateway not running';
  const body = notConfigured
    ? 'In Hermes WebUI, scheduled jobs require the Hermes gateway daemon. If this is a single-container Docker install, jobs can be created and run manually here, but scheduled ticks need a gateway container or `hermes gateway` running outside the WebUI.'
    : 'In Hermes WebUI, scheduled jobs require the Hermes gateway daemon to be running. Start the gateway container or `hermes gateway` before relying on offline scheduled runs.';
  return `
    <div class="detail-alert-title">${esc(title)}</div>
    <p>${esc(body)}</p>
  `;
}

async function loadCronGatewayNotice() {
  const box = $('cronGatewayNotice');
  if (!box) return;
  try {
    const status = await api('/api/gateway/status');
    const html = _cronGatewayNoticeHtml(status);
    if (html) {
      box.innerHTML = html;
      box.style.display = '';
    } else {
      box.innerHTML = '';
      box.style.display = 'none';
    }
  } catch (_) {
    box.innerHTML = '';
    box.style.display = 'none';
  }
}

async function loadCrons(animate) {
  const box = $('cronList');
  const refreshBtn = $('cronRefreshBtn');
  loadCronGatewayNotice();
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
      const isAgentMode = !job.no_agent;
      const profileLabel = _cronProfileLabel(job.profile);
      const profileTitle = _cronProfileTitle(job.profile);
      item.innerHTML = `
        <div class="cron-header">
          ${isNewRun ? '<span class="cron-new-dot" title="New run"></span>' : ''}
          ${isAgentMode ? '<span class="cron-agent-badge" title="Agent mode">🤖</span>' : ''}
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

function _cronPanelExpandKey(jobId, suffix){
  return `hermes-webui-cron-${suffix}-expanded-${encodeURIComponent(String(jobId||''))}`;
}

function _cronRunExpandKey(jobId, filename){
  return `${_cronPanelExpandKey(jobId, 'run')}-${encodeURIComponent(String(filename||''))}`;
}

function _cronExpansionGet(key){
  try { return localStorage.getItem(key) === '1'; } catch(_) { return false; }
}

function _cronExpansionSet(key, expanded){
  try { localStorage.setItem(key, expanded ? '1' : '0'); } catch(_) {}
}

function toggleCronPromptExpanded(jobId){
  const key = _cronPanelExpandKey(jobId, 'prompt');
  _cronExpansionSet(key, !_cronExpansionGet(key));
  if (_currentCronDetail && String(_currentCronDetail.id) === String(jobId)) {
    _renderCronDetail(_currentCronDetail);
  }
}

function toggleCronRunExpanded(jobId, filename, runId){
  const key = _cronRunExpandKey(jobId, filename);
  const expanded = !_cronExpansionGet(key);
  _cronExpansionSet(key, expanded);
  const item = document.getElementById(runId);
  const body = item ? item.querySelector('.detail-run-body') : null;
  const btn = item ? item.querySelector('.detail-expand-toggle') : null;
  if (body) body.classList.toggle('expanded', expanded);
  if (btn) {
    btn.textContent = expanded ? '▴' : '▾';
    btn.title = expanded ? (t('cron_collapse_output') || 'Collapse output') : (t('cron_expand_output') || 'Expand output');
    btn.setAttribute('aria-label', btn.title);
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
  const isNoAgent = !!job.no_agent;
  const cronJobMode = isNoAgent ? 'no-agent' : 'agent';
  const modelProvider =
    job.provider && job.model ? `${esc(job.provider)}/${esc(job.model)}` :
    job.model ? esc(job.model) :
    job.provider ? esc(job.provider) :
    isNoAgent ? '' : 'default';
  const script = job.script || '';
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
  const toastNotifications = job.toast_notifications !== false;
  const promptExpanded = _cronExpansionGet(_cronPanelExpandKey(job.id, 'prompt'));
  const promptToggleLabel = promptExpanded ? (t('cron_collapse_prompt') || 'Collapse prompt') : (t('cron_expand_prompt') || 'Expand prompt');
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
        <div class="detail-row"><div class="detail-row-label">Mode</div><div class="detail-row-value"><span class="detail-badge" id="cronJobMode">${esc(cronJobMode)}</span>${modelProvider ? ` <code>${modelProvider}</code>` : ''}</div></div>
        ${isNoAgent ? `<div class="detail-row"><div class="detail-row-label">No-agent script</div><div class="detail-row-value"><code>${esc(script || '—')}</code></div></div>` : ''}
        <div class="detail-row"><div class="detail-row-label">${esc(t('cron_profile_label') || 'Profile')}</div><div class="detail-row-value"><span class="detail-badge active" title="${esc(profileTitle)}">${esc(profileLabel)}</span></div></div>
        <div class="detail-row"><div class="detail-row-label">${esc(t('cron_toast_notifications_label') || 'Completion toasts')}</div><div class="detail-row-value"><span class="detail-badge ${toastNotifications ? 'active' : ''}">${esc(toastNotifications ? (t('cron_toast_notifications_enabled') || 'Enabled') : (t('cron_toast_notifications_disabled') || 'Disabled'))}</span></div></div>
        <div class="detail-row"><div class="detail-row-label">Skills</div><div class="detail-row-value">${esc(skills)}</div></div>
        ${lastError}
      </div>
      <div class="detail-card">
        <div class="detail-card-title detail-card-title-row">
          <span>Prompt</span>
          <button type="button" class="detail-expand-toggle" onclick="toggleCronPromptExpanded('${esc(job.id)}')" title="${esc(promptToggleLabel)}" aria-label="${esc(promptToggleLabel)}">${esc(promptExpanded ? '▴' : '▾')}</button>
        </div>
        <div class="detail-prompt ${promptExpanded ? 'expanded' : ''}">${esc(job.prompt || '')}</div>
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
      const usageStrip = _formatCronRunUsageStrip(run.usage);
      const runExpanded = _cronExpansionGet(_cronRunExpandKey(jobId, run.filename));
      const runToggleLabel = runExpanded ? (t('cron_collapse_output') || 'Collapse output') : (t('cron_expand_output') || 'Expand output');
      return `<div class="detail-run-item" id="${rid}">
        <div class="detail-run-head" onclick="_loadRunContent('${esc(jobId)}','${esc(run.filename)}','${rid}')">
          <span><span style="opacity:.7">${esc(ts)}</span> <span style="opacity:.4;font-size:11px">${esc(sizeStr)}</span>${usageStrip ? ` <span class="cron-run-usage-strip">${esc(usageStrip)}</span>` : ''}</span>
          <span class="detail-run-actions">
            <button type="button" class="detail-expand-toggle" onclick="event.stopPropagation();toggleCronRunExpanded('${esc(jobId)}','${esc(run.filename)}','${rid}')" title="${esc(runToggleLabel)}" aria-label="${esc(runToggleLabel)}">${esc(runExpanded ? '▴' : '▾')}</button>
            <span style="opacity:.6">▸</span>
          </span>
        </div>
        <div class="detail-run-body ${runExpanded ? 'expanded' : ''}" style="color:var(--muted);font-size:12px">${esc(t('loading'))}</div>
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
  body.classList.toggle('expanded', _cronExpansionGet(_cronRunExpandKey(jobId, filename)));
  body.innerHTML = `<span style="opacity:.5">${esc(t('loading'))}</span>`;
  try {
    const data = await api(`/api/crons/run?job_id=${encodeURIComponent(jobId)}&filename=${encodeURIComponent(filename)}`);
    if (data.error) {
      body.textContent = data.error;
      return;
    }
    const expanded = _cronExpansionGet(_cronRunExpandKey(jobId, filename));
    const output = expanded ? (data.content || data.snippet || '') : (data.snippet || data.content || '');
    body.classList.toggle('expanded', expanded);
    // Render markdown content using the same renderer as chat messages
    if (typeof renderMd === 'function') {
      body.innerHTML = renderMd(output);
    } else {
      body.textContent = output;
    }
    const usageStrip = _formatCronRunUsageStrip(data.usage);
    if (usageStrip) {
      const usage = document.createElement('div');
      usage.className = 'cron-run-usage-strip cron-run-usage-footer';
      usage.textContent = usageStrip;
      body.appendChild(usage);
    }
    // Show "View full output" button only for collapsed previews. Expanded rows render the full body inline.
    if (!expanded && data.content && data.snippet && data.content.length > data.snippet.length) {
      const btn = document.createElement('button');
      btn.style.cssText = 'margin-top:8px;padding:4px 12px;border-radius:var(--radius-btn);border:1px solid var(--border-subtle);background:var(--surface-subtle);color:var(--text-secondary);cursor:pointer;font-size:12px';
      btn.textContent = t('cron_view_full_output') || 'View full output';
      btn.onclick = () => {
        _cronExpansionSet(_cronRunExpandKey(jobId, filename), true);
        body.classList.add('expanded');
        body.innerHTML = renderMd ? renderMd(data.content) : data.content;
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
    toast_notifications: job.toast_notifications !== false,
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
  _renderCronForm({ name:'', schedule:'', prompt:'', deliver:'local', profile:'', toast_notifications:true, isEdit:false });
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
    toast_notifications: job.toast_notifications !== false,
    no_agent: !!job.no_agent,
    script: job.script || '',
    isEdit: true,
  });
  if (!_cronSkillsCache) {
    api('/api/skills').then(d=>{_cronSkillsCache=d.skills||[]; _bindCronSkillPicker();}).catch(()=>{});
  } else {
    _bindCronSkillPicker();
  }
  loadCronProfiles().then(()=>_refreshCronProfileSelect(job.profile || '')).catch(()=>{});
}

function _renderCronForm({ name, schedule, prompt, deliver, profile, toast_notifications=true, no_agent=false, script='', isEdit }){
  const title = $('taskDetailTitle');
  const body = $('taskDetailBody');
  const empty = $('taskDetailEmpty');
  if (!body || !title) return;
  const isNoAgent = !!no_agent;
  const toastNotifications = toast_notifications !== false;
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
          <div id="cronFormScheduleOnceWarning" class="detail-form-warning cron-once-warning" style="display:none">${esc(t('cron_schedule_once_warning') || "Duration forms like '30m' run once and are removed after running. Use 'every 30m' to keep a recurring job.")}</div>
        </div>
        <div class="detail-form-row ${isNoAgent ? 'cron-no-agent-prompt-row' : ''}">
          <label for="cronFormPrompt">${esc(t('cron_prompt_label') || 'Prompt')}</label>
          <textarea id="cronFormPrompt" rows="6" placeholder="${esc(t('cron_prompt_placeholder') || 'Must be self-contained')}"${isNoAgent ? ' disabled' : ' required'}>${esc(prompt || '')}</textarea>
          ${isNoAgent ? `<div class="detail-form-hint cron-no-agent-hint">No-agent mode runs the configured script directly; Prompt is unused. No-agent script: <code>${esc(script || '—')}</code></div>` : ''}
        </div>
        <div class="detail-form-row">
          <label for="cronFormDeliver">${esc(t('cron_deliver_label') || 'Deliver output to')}</label>
          <select id="cronFormDeliver" ${isEdit ? 'disabled' : ''}>
            ${deliverOpt('local', t('cron_deliver_local') || 'Local (save output only)')}
            ${deliverOpt('discord','Discord')}
            ${deliverOpt('telegram','Telegram')}
            ${deliverOpt('slack','Slack')}
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
          <label for="cronFormToastNotifications">${esc(t('cron_toast_notifications_label') || 'Completion toasts')}</label>
          <label class="detail-form-check" for="cronFormToastNotifications">
            <input type="checkbox" id="cronFormToastNotifications" ${toastNotifications ? 'checked' : ''}>
            <span>${esc(t('cron_toast_notifications_hint') || 'Show a toast when this cron finishes.')}</span>
          </label>
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
  const scheduleEl = $('cronFormSchedule');
  if (scheduleEl) {
    scheduleEl.addEventListener('input', _syncCronScheduleWarning);
    scheduleEl.addEventListener('change', _syncCronScheduleWarning);
    _syncCronScheduleWarning();
  }
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
  const toastEl=$('cronFormToastNotifications');
  const errEl=$('cronFormError');
  if(!schEl||!promptEl||!errEl) return;
  const name=(nameEl?nameEl.value:'').trim();
  const schedule=schEl.value.trim();
  const prompt=promptEl.value.trim();
  const deliver=delivEl?delivEl.value:'local';
  const profile=profileEl?profileEl.value:'';
  const toastNotifications=toastEl?!!toastEl.checked:true;
  const isNoAgent = !!(_cronPreFormDetail && _cronPreFormDetail.no_agent);
  errEl.style.display='none';
  if(!schedule){errEl.textContent=t('cron_schedule_required_example');errEl.style.display='';return;}
  if(!isNoAgent && !prompt){errEl.textContent=t('cron_prompt_required');errEl.style.display='';return;}
  try{
    if (_editingCronId) {
      const updates = {job_id: _editingCronId, schedule, profile: profile, toast_notifications: toastNotifications};
      if (!isNoAgent) updates.prompt = prompt;
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
    const body={schedule,prompt,deliver,profile: profile, toast_notifications: toastNotifications};
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

function _formatCronRunUsageStrip(usage) {
  if (!usage || typeof usage !== 'object') return '';
  const parts = [];
  const fmt = n => {
    const value = Number(n || 0);
    if (!Number.isFinite(value) || value <= 0) return '';
    if (value >= 1000000) return (value / 1000000).toFixed(value >= 10000000 ? 0 : 1).replace(/\.0$/, '') + 'M';
    if (value >= 1000) return (value / 1000).toFixed(value >= 10000 ? 0 : 1).replace(/\.0$/, '') + 'k';
    return String(Math.round(value));
  };
  const input = fmt(usage.input_tokens);
  const output = fmt(usage.output_tokens);
  const total = fmt(usage.total_tokens);
  if (input || output) parts.push(`${input || '0'} in · ${output || '0'} out`);
  else if (total) parts.push(`${total} tokens`);
  const cost = Number(usage.estimated_cost_usd);
  if (Number.isFinite(cost) && cost > 0) parts.push(`$${cost < 0.01 ? cost.toFixed(4) : cost.toFixed(3)}`);
  if (usage.model) parts.push(String(usage.model));
  return parts.join(' · ');
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


/**
 * Render inline markdown (bold, italic, code, links, strikethrough).
 * Input is already HTML-escaped.
 */
function _kanbanRenderMarkdownInline(escaped){
  return String(escaped || '')
    .replace(/~~([^~\n]+)~~/g, (_m, text) => `<del>${text}</del>`)
    .replace(/`([^`\n]+)`/g, (_m, code) => `<code>${code}</code>`)
    .replace(/\*\*([^*\n]+)\*\*/g, (_m, text) => `<strong>${text}</strong>`)
    .replace(/(^|[^*a-zA-Z0-9])\*([^*\n]+)\*/g, (_m, prefix, text) => `${prefix}<em>${text}</em>`)
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g, (_m, text, href) => `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`);
}

/**
 * Render full markdown block content: headings, code blocks, lists, tables,
 * task lists, blockquotes, horizontal rules, paragraphs + inline formatting.
 */
function _kanbanRenderMarkdown(source){
  if (!source) return '';
  const lines = esc(source).split(/\r?\n/);
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // ── Code block ──
    if (/^```/.test(trimmed)) {
      const lang = trimmed.slice(3).trim();
      const codeLines = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // skip closing ```
      const codeHtml = codeLines.join('\n');
      out.push(lang
        ? `<pre class="hermes-kanban-code"><code class="language-${_kanbanRenderMarkdownInline(lang)}">${codeHtml}</code></pre>`
        : `<pre class="hermes-kanban-code"><code>${codeHtml}</code></pre>`);
      continue;
    }

    // ── Horizontal rule ──
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(trimmed)) {
      out.push('<hr>');
      i++;
      continue;
    }

    // ── Heading ──
    const headingMatch = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      out.push(`<h${level}>${_kanbanRenderMarkdownInline(headingMatch[2])}</h${level}>`);
      i++;
      continue;
    }

    // ── Blockquote ──
    if (/^>\s?/.test(trimmed)) {
      const quoteLines = [];
      while (i < lines.length && /^>\s?/.test(lines[i].trim())) {
        quoteLines.push(lines[i].trim().replace(/^>\s?/, ''));
        i++;
      }
      out.push(`<blockquote>${_kanbanRenderMarkdownInline(quoteLines.join('<br>'))}</blockquote>`);
      continue;
    }

    // ── Table row ──
    if (/^\|.+\|$/.test(trimmed)) {
      const tableRows = [];
      const tableAligns = [];
      while (i < lines.length && /^\|.+\|$/.test(lines[i].trim())) {
        const row = lines[i].trim();
        // Detect alignment separator row
        if (/^\|[\s:]*-{3,}[\s:]*\|/.test(row)) {
          const cells = row.split('|').filter(c => c.trim().length > 0);
          cells.forEach(c => {
            const t = c.trim();
            if (t.startsWith(':') && t.endsWith(':')) tableAligns.push('center');
            else if (t.endsWith(':')) tableAligns.push('right');
            else tableAligns.push('left');
          });
        } else {
          const cells = row.split('|').filter(c => c.trim().length > 0);
          tableRows.push(cells.map((c, ci) => {
            const align = tableAligns[ci] ? ` style="text-align:${tableAligns[ci]}"` : '';
            return `<td${align}>${_kanbanRenderMarkdownInline(c.trim())}</td>`;
          }).join(''));
        }
        i++;
      }
      if (tableRows.length) {
        out.push(`<table><tbody>${tableRows.map(r => `<tr>${r}</tr>`).join('')}</tbody></table>`);
      }
      continue;
    }

    // ── Task list item ──
    const taskMatch = trimmed.match(/^[-*+]\s+\[( |x|X)\]\s+(.+)$/);
    if (taskMatch) {
      const checked = taskMatch[1] !== ' ';
      const text = taskMatch[2];
      const items = [];
      items.push(`<li class="hermes-kanban-task${checked ? ' checked' : ''}"><input type="checkbox"${checked ? ' checked' : ''} disabled> ${_kanbanRenderMarkdownInline(text)}</li>`);
      i++;
      // Collect continuation items
      while (i < lines.length) {
        const next = lines[i].trim();
        const nextTask = next.match(/^[-*+]\s+\[( |x|X)\]\s+(.+)$/);
        const nextLi = next.match(/^[-*+]\s+(.+)$/);
        if (nextTask) {
          const c = nextTask[1] !== ' ';
          items.push(`<li class="hermes-kanban-task${c ? ' checked' : ''}"><input type="checkbox"${c ? ' checked' : ''} disabled> ${_kanbanRenderMarkdownInline(nextTask[2])}</li>`);
          i++;
        } else if (nextLi) {
          items.push(`<li>${_kanbanRenderMarkdownInline(nextLi[1])}</li>`);
          i++;
        } else {
          break;
        }
      }
      out.push(`<ul>${items.join('')}</ul>`);
      continue;
    }

    // ── Unordered list item ──
    const ulMatch = trimmed.match(/^[-*+]\s+(.+)$/);
    if (ulMatch) {
      const items = [];
      items.push(`<li>${_kanbanRenderMarkdownInline(ulMatch[1])}</li>`);
      i++;
      while (i < lines.length) {
        const next = lines[i].trim();
        const nextUl = next.match(/^[-*+]\s+(.+)$/);
        const nextTask = next.match(/^[-*+]\s+\[( |x|X)\]\s+(.+)$/);
        if (nextTask) break; // let task list handler get it
        if (nextUl) {
          items.push(`<li>${_kanbanRenderMarkdownInline(nextUl[1])}</li>`);
          i++;
        } else {
          break;
        }
      }
      out.push(`<ul>${items.join('')}</ul>`);
      continue;
    }

    // ── Ordered list item ──
    const olMatch = trimmed.match(/^\d+\.\s+(.+)$/);
    if (olMatch) {
      const items = [];
      items.push(`<li>${_kanbanRenderMarkdownInline(olMatch[1])}</li>`);
      i++;
      while (i < lines.length) {
        const next = lines[i].trim();
        const nextOl = next.match(/^\d+\.\s+(.+)$/);
        if (nextOl) {
          items.push(`<li>${_kanbanRenderMarkdownInline(nextOl[1])}</li>`);
          i++;
        } else {
          break;
        }
      }
      out.push(`<ol>${items.join('')}</ol>`);
      continue;
    }

    // ── Empty line ──
    if (!trimmed) {
      out.push('');
      i++;
      continue;
    }

    // ── Paragraph ──
    out.push(`<p>${_kanbanRenderMarkdownInline(trimmed)}</p>`);
    i++;
  }
  return `<div class="hermes-kanban-md">${out.join('\n')}</div>`;
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
  const complete = status !== 'done' && status !== 'archived' ? `<button type="button" class="kanban-card-action" onclick="quickKanbanCardAction(event,'${id}','done')">${esc(t('kanban_card_complete'))}</button>` : '';
  const archive = status !== 'archived' ? `<button type="button" class="kanban-card-action danger" onclick="quickKanbanCardAction(event,'${id}','archived')">${esc(t('kanban_card_archive'))}</button>` : '';
  return `<div class="kanban-card-actions" onclick="event.stopPropagation()">${complete}${archive}</div>`;
}

async function quickKanbanCardAction(event, taskId, status){
  if (event) event.stopPropagation();
  return updateKanbanTask(taskId, {status});
}

function _kanbanSuppressNextCardClick(){
  _kanbanSuppressCardClickUntil = Date.now() + 700;
}

function dragKanbanTask(event, taskId){
  _kanbanSuppressNextCardClick();
  if (!event.dataTransfer) return;
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', taskId);
}

function finishKanbanDrag(event){
  if (event) _kanbanSuppressNextCardClick();
}

function openKanbanCard(event, taskId){
  if (Date.now() < _kanbanSuppressCardClickUntil) {
    if (event) {
      event.preventDefault();
      event.stopPropagation();
    }
    return false;
  }
  loadKanbanTask(taskId);
  return false;
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
  _kanbanSuppressNextCardClick();
  event.preventDefault();
  event.stopPropagation();
  clearKanbanDrop(event);
  const taskId = event.dataTransfer ? event.dataTransfer.getData('text/plain') : '';
  if (taskId && status) await updateKanbanTask(taskId, {status}, {openDetail: false});
  _kanbanSuppressNextCardClick();
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

function _kanbanEmptyBoardHtml(){
  return `<div class="main-view-empty"><div class="main-view-empty-title">${esc(t('kanban_no_data'))}</div><div class="main-view-empty-sub">${esc(t('kanban_work_queue_hint'))}</div></div>`;
}

function _kanbanRenderBoard(){
  const board = $('kanbanBoard');
  if (!board) return;
  if (!_kanbanBoard || !_kanbanBoard.columns) {
    board.innerHTML = _kanbanEmptyBoardHtml();
    return;
  }
  const columns = _kanbanVisibleTasks();
  const total = columns.reduce((n, col) => n + (col.tasks || []).length, 0);
  if ($('kanbanSummary')) $('kanbanSummary').textContent = String(t('kanban_visible_tasks')).replace('{0}', total);
  _kanbanRenderSidebar(columns);
  if (total === 0) {
    board.innerHTML = _kanbanEmptyBoardHtml();
    return;
  }
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
  return `<article class="kanban-card ${esc(stale)}" data-kanban-task-id="${esc(task.id)}" draggable="true" ondragstart="dragKanbanTask(event, '${esc(task.id)}')" ondragend="finishKanbanDrag(event)" onclick="return openKanbanCard(event, '${esc(task.id)}')" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();loadKanbanTask('${esc(task.id)}')}">
    <div class="kanban-card-topline"><span class="kanban-card-id">${esc(task.id || '')}</span>${priority ? `<span class="kanban-badge priority">P${priority}</span>` : ''}${task.tenant ? `<span class="kanban-badge tenant">${esc(task.tenant)}</span>` : ''}</div>
    <div class="kanban-card-title">${esc(_kanbanTaskTitle(task))}</div>
    ${body ? `<div class="kanban-card-body">${_kanbanRenderMarkdown(body)}</div>` : ''}
    <div class="kanban-card-meta">${assignee}${comments ? `<span class="kanban-card-metric">💬 ${comments}</span>` : ''}${linkTotal ? `<span class="kanban-card-metric">↔ ${linkTotal}</span>` : ''}${age ? `<span class="kanban-card-age">${esc(age)}</span>` : ''}</div>
    ${_kanbanCardQuickActions(task)}
  </article>`;
}

async function hardRefreshWebUIClient(){
  try {
    if (navigator.serviceWorker) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map(r => r.unregister()));
    }
  } catch(_) {}
  try {
    if (window.caches) {
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k)));
    }
  } catch(_) {}
  window.location.reload();
}

function _kanbanLooksLikeStaleClientError(err){
  const msg = String((err && err.message) || err || '').toLowerCase();
  return !!(err && err.status === 404 && (
    msg === 'not found' ||
    msg.includes('unknown kanban endpoint') ||
    msg.includes('stale cached bundle')
  ));
}

function _kanbanUnavailableHtml(err){
  const raw = String((err && err.message) || err || '');
  if (_kanbanLooksLikeStaleClientError(err)) {
    return `<div class="main-view-empty"><div class="main-view-empty-title">Kanban needs a hard refresh</div><div class="main-view-empty-subtitle">The server rejected an obsolete Kanban endpoint. This usually means the browser or Mac app is still running a stale cached WebUI bundle after an update.</div><button class="btn primary" type="button" onclick="hardRefreshWebUIClient()">Hard refresh now</button><div class="main-view-empty-subtitle">Original error: ${esc(raw || 'not found')}</div></div>`;
  }
  const msg = `${esc(t('kanban_unavailable'))}: ${esc(raw)}`;
  return `<div class="main-view-empty"><div class="main-view-empty-title">${msg}</div></div>`;
}

async function loadKanban(animate){
  const board = $('kanbanBoard');
  const list = $('kanbanList');
  try {
    if (animate && board) board.innerHTML = `<div style="padding:16px;color:var(--muted);font-size:13px">${esc(t('loading'))}</div>`;
    // Resolve the active board before board-scoped requests. If another CLI or
    // tab archived the previous board, /boards can fall back to default instead
    // of leaving config/board pinned to a ghost slug.
    await loadKanbanBoards();
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
    // Note: PR #1828 (v0.51.20) moved the boards refresh to the start of
    // loadKanban() so the active board is resolved BEFORE board-scoped
    // requests fire. The previous tail-of-function refresh has been removed
    // to avoid doubling /api/kanban/boards traffic during SSE-driven
    // refreshes (debounced at 250ms via _scheduleKanbanRefresh). The
    // 30-second poll started by _kanbanStartPolling() picks up any board
    // state changes that arrive after this render.
    _kanbanStartPolling();
    _kanbanRenderBoard();
  } catch(e) {
    const html = _kanbanUnavailableHtml(e);
    if (board) board.innerHTML = html;
    if (list) list.innerHTML = html;
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
  if (_kanbanIsDispatching) return;
  // Dry-run dispatch: show what WOULD be spawned, without actually spawning
  // workers.  Uses ?dry_run=1 so the dispatcher reports its plan without
  // mutating the board.  The result shape includes spawned/skipped_unassigned/
  // skipped_nonspawnable/promoted/auto_blocked so users can diagnose why a
  // Ready task isn't being picked up before they commit to a real run.
  _kanbanIsDispatching = true;
  _setKanbanDispatcherButtonsDisabled(true);
  try {
    const dispatchEndpoint = '/api/kanban/dispatch';
    const result = await api(
      dispatchEndpoint + '?dry_run=1&max=8' + (_kanbanCurrentBoard ? '&board=' + encodeURIComponent(_kanbanCurrentBoard) : ''),
      {method: 'POST'},
    );
    showToast(_kanbanFormatDispatchResult(result, true), 'info', 6000);
    await loadKanban(true);
  } catch(e) {
    showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error');
  } finally {
    _kanbanIsDispatching = false;
    _setKanbanDispatcherButtonsDisabled(false);
  }
}

async function runKanbanDispatcher(){
  if (_kanbanIsDispatching) return;
  // Real dispatch: claims Ready tasks and spawns worker subprocesses
  // (one `hermes -p <assignee>` per claimed row, up to max=8 per call).
  // Confirmation dialog first because this actually consumes API budget on
  // each spawned worker.  Result toast surfaces what happened so users see
  // the dispatcher actually doing work.
  if (!_kanbanCurrentBoard) {
    showToast(t('kanban_unavailable') || 'Kanban unavailable', 'error');
    return;
  }

  _kanbanIsDispatching = true;
  _setKanbanDispatcherButtonsDisabled(true);
  try {
    const ok = await showConfirmDialog({
      title: t('kanban_run_dispatcher') || 'Run dispatcher',
      message: t('kanban_run_dispatcher_confirm')
        || 'This will claim Ready tasks on this board and spawn worker subprocesses (one per task, up to 8 per click). Continue?',
      confirmLabel: t('kanban_run_dispatcher') || 'Run dispatcher',
    });
    if (!ok) return;
    const dispatchEndpoint = '/api/kanban/dispatch';
    const result = await api(
      dispatchEndpoint + '?max=8' + (_kanbanCurrentBoard ? '&board=' + encodeURIComponent(_kanbanCurrentBoard) : ''),
      {method: 'POST'},
    );
    showToast(_kanbanFormatDispatchResult(result, false), 'info', 8000);
    await loadKanban(true);
  } catch(e) {
    showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error');
  } finally {
    _kanbanIsDispatching = false;
    _setKanbanDispatcherButtonsDisabled(false);
  }
}

function _setKanbanDispatcherButtonsDisabled(disabled){
  document.querySelectorAll('.kanban-run-dispatch-btn, .kanban-nudge-dispatch-btn').forEach((btn) => {
    btn.disabled = !!disabled;
    btn.classList.toggle('disabled', !!disabled);
  });
}

function _kanbanFormatDispatchResult(result, dryRun){
  // Produce a human-readable one-line summary of dispatch_once's output so
  // users can see exactly what happened rather than a generic "OK" toast.
  const r = result || {};
  const spawned = (r.spawned || []).length;
  const promoted = r.promoted || 0;
  const reclaimed = r.reclaimed || 0;
  const skippedUnassigned = (r.skipped_unassigned || []).length;
  const skippedNonspawnable = (r.skipped_nonspawnable || []).length;
  const autoBlocked = (r.auto_blocked || []).length;
  const timedOut = (r.timed_out || []).length;
  const crashed = (r.crashed || []).length;
  const verb = dryRun ? (t('kanban_dispatch_preview_prefix') || 'Preview:') : (t('kanban_dispatch_run_prefix') || 'Dispatched:');
  const parts = [];
  parts.push(spawned + ' ' + (t('kanban_dispatch_spawned') || 'spawned'));
  if (promoted) parts.push(promoted + ' ' + (t('kanban_dispatch_promoted') || 'promoted'));
  if (reclaimed) parts.push(reclaimed + ' ' + (t('kanban_dispatch_reclaimed') || 'reclaimed'));
  if (skippedUnassigned) parts.push(skippedUnassigned + ' ' + (t('kanban_dispatch_skipped_unassigned') || 'skipped (no assignee)'));
  if (skippedNonspawnable) parts.push(skippedNonspawnable + ' ' + (t('kanban_dispatch_skipped_nonspawnable') || 'skipped (unknown profile)'));
  if (autoBlocked) parts.push(autoBlocked + ' ' + (t('kanban_dispatch_auto_blocked') || 'auto-blocked'));
  if (timedOut) parts.push(timedOut + ' ' + (t('kanban_dispatch_timed_out') || 'timed out'));
  if (crashed) parts.push(crashed + ' ' + (t('kanban_dispatch_crashed') || 'crashed'));
  return verb + ' ' + parts.join(', ');
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
    <div class="kanban-detail-row-main">${_kanbanRenderMarkdown(body)}</div>
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
  if (!title) {
    // Empty inline input (or a click on the panel-head "+" via openKanbanCreate)
    // — open the full create-task modal so the user has somewhere obvious to
    // type and configure the task. Mirrors the cron / skills pattern of routing
    // header "+" clicks through to a clearly-modal create surface.
    openKanbanCreate();
    return;
  }
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

// ────────────────────────────────────────────────────────────────────────────
// Kanban: create-task modal (panel-head "+" button entry point).
//
// Same `.kanban-modal-overlay` shell as openKanbanCreateBoard() so the two
// flows look and behave identically (centered card, dim backdrop, ESC closes,
// click-on-backdrop closes). The modal markup lives in static/index.html as
// #kanbanTaskModal — see the section just above </body>. Submit hits the
// existing /api/kanban/tasks POST endpoint (which already accepts title, body,
// assignee, tenant, priority, status — see api/kanban_bridge.py:306).
// ────────────────────────────────────────────────────────────────────────────

// ────────────────────────────────────────────────────────────────────────────
// Kanban: create-task / edit-task modal (panel-head "+" + task-detail Edit
// button entry points).
//
// Single modal serves both flows.  Title + submit-button labels and the
// underlying submit verb (POST vs PATCH) flip based on `_kanbanTaskModalMode`.
//
// Same `.kanban-modal-overlay` shell as openKanbanCreateBoard() so the two
// flows look and behave identically (centered card, dim backdrop, ESC closes,
// click-on-backdrop closes). The modal markup lives in static/index.html as
// #kanbanTaskModal — see the section just above </body>.
//
// The assignee field auto-completes against the union of (a) live Hermes
// profile names from /api/profiles and (b) historical assignees on the
// active board, with an inline hint that explains the dispatcher claim
// contract — most users will pick a profile name from the dropdown rather
// than type one.
// ────────────────────────────────────────────────────────────────────────────

let _kanbanTaskModalMode = 'create';   // 'create' | 'edit'
let _kanbanTaskModalEditingId = null;  // task id when mode === 'edit'
let _kanbanProfileNamesCache = null;   // populated lazily on first modal open
let _kanbanProfileNamesCacheAt = 0;
const _KANBAN_PROFILE_NAMES_CACHE_TTL_MS = 30000;
function _invalidateKanbanProfileCache() {
  _kanbanProfileNamesCache = null;
  _kanbanProfileNamesCacheAt = 0;
}
let _kanbanTaskModalFocusCleanup = null;
// Status the modal *displayed* on edit-mode open.  If the user doesn't touch
// the dropdown, we must NOT send `status` in the PATCH payload — otherwise
// editing a task whose real status is non-editable in this dropdown
// (running/blocked/done/archived → mapped to 'triage' for display) would
// silently demote the task on save.  See the regression caught during PR
// review: editing a 'running' task without touching status was reclaiming
// the worker and moving the task back to triage.
let _kanbanTaskModalInitialDisplayedStatus = null;
let _kanbanBoardModalFocusCleanup = null;

async function _kanbanLoadProfileNames(){
  // Hit /api/profiles once per session and cache for a short TTL.
  // Returns an array of profile names (sorted, default first if present).
  const hasFreshCache = (
    Array.isArray(_kanbanProfileNamesCache) &&
    (Date.now() - _kanbanProfileNamesCacheAt) < _KANBAN_PROFILE_NAMES_CACHE_TTL_MS
  );
  if (hasFreshCache) return _kanbanProfileNamesCache;
  try {
    const data = await api('/api/profiles');
    const profiles = Array.isArray(data && data.profiles) ? data.profiles : [];
    const names = profiles.map(p => p && p.name).filter(Boolean);
    // Stable order: default first, then alphabetical.
    names.sort((a, b) => {
      if (a === 'default') return -1;
      if (b === 'default') return 1;
      return a.localeCompare(b);
    });
    _kanbanProfileNamesCache = names;
    _kanbanProfileNamesCacheAt = Date.now();
    return names;
  } catch(_) {
    _kanbanProfileNamesCache = [];
    _kanbanProfileNamesCacheAt = Date.now();
    return [];
  }
}

async function _kanbanPopulateAssigneeSelect(currentValue){
  const sel = document.getElementById('kanbanTaskModalAssignee');
  if (!sel) return;
  // Profile names: the canonical set the dispatcher can claim.
  const profileNames = await _kanbanLoadProfileNames();
  // Historical assignees from the active board: include them so users who
  // assigned to a CLI lane (e.g. orion-cc) before still see those values.
  const historicalAssignees = (_kanbanBoard && Array.isArray(_kanbanBoard.assignees))
    ? _kanbanBoard.assignees
    : [];
  // Build a final ordered list, deduping.  Profiles come first, then any
  // historical assignees that aren't profiles (rare but keeps round-tripping
  // correct for tasks created via CLI).
  const seen = new Set();
  const profiles = [];
  for (const name of profileNames) {
    if (!seen.has(name)) { profiles.push(name); seen.add(name); }
  }
  const extras = [];
  for (const name of historicalAssignees) {
    if (name && !seen.has(name)) { extras.push(name); seen.add(name); }
  }
  // If the current value isn't in either bucket (e.g. an old CLI-created
  // assignee that's since been deleted), preserve it as a final option so
  // editing the task doesn't silently change its assignee.
  if (currentValue && !seen.has(currentValue)) {
    extras.push(currentValue);
    seen.add(currentValue);
  }
  // The empty value maps to null on submit (intentionally unassigned).  Keep
  // it last so the default-selected option is the first profile, not "no one".
  let html = '';
  if (profiles.length) {
    html += `<optgroup label="${esc(t('kanban_assignee_profiles_label') || 'Hermes profiles')}">`;
    html += profiles.map(v => `<option value="${esc(v)}"${v === currentValue ? ' selected' : ''}>${esc(v)}</option>`).join('');
    html += '</optgroup>';
  }
  if (extras.length) {
    html += `<optgroup label="${esc(t('kanban_assignee_other_label') || 'Other (CLI lanes / removed profiles)')}">`;
    html += extras.map(v => `<option value="${esc(v)}"${v === currentValue ? ' selected' : ''}>${esc(v)}</option>`).join('');
    html += '</optgroup>';
  }
  // Final "no assignee" fallthrough — explicit so users know what they're choosing.
  html += `<option value=""${(!currentValue) ? ' selected' : ''}>${esc(t('kanban_assignee_unassigned') || '— Unassigned (won\u2019t auto-run) —')}</option>`;
  sel.innerHTML = html;
}

function openKanbanCreate(){
  // Make sure the user is on the kanban panel so the resulting board reload is
  // visible behind the modal.
  if (typeof switchPanel === 'function' && _currentPanel !== 'kanban') switchPanel('kanban');
  const modal = document.getElementById('kanbanTaskModal');
  if (!modal) return;
  _kanbanTaskModalMode = 'create';
  _kanbanTaskModalEditingId = null;
  _kanbanTaskModalInitialDisplayedStatus = null;  // create mode: always send status
  // Default new tasks to "ready" so they're immediately claimable by the
  // dispatcher (assuming the user picks an assignee).  Triage is for staging
  // tasks that need human review before being marked actionable; users who
  // want it can still pick it from the status dropdown.
  _kanbanResetTaskModalFields({status: 'ready'});
  _kanbanSetTaskModalStatusHint(null);
  _kanbanSetTaskModalLabels('create');
  _kanbanPopulateAssigneeSelect('').then(() => {
    // After the dropdown is populated, default-select the first profile (not
    // the "Unassigned" fallthrough).  This is the right hint: most users want
    // to assign to *something* — they can pick "Unassigned" deliberately.
    const sel = document.getElementById('kanbanTaskModalAssignee');
    if (sel && sel.options.length > 0 && sel.value === '') {
      const firstProfile = Array.from(sel.options).find(opt => opt.value !== '');
      if (firstProfile) sel.value = firstProfile.value;
    }
  });
  _kanbanPopulateTenantDatalist();
  modal.hidden = false;
  if (_kanbanTaskModalFocusCleanup) {
    _kanbanTaskModalFocusCleanup();
    _kanbanTaskModalFocusCleanup = null;
  }
  _kanbanTaskModalFocusCleanup = _trapModalFocus(modal);
  setTimeout(() => {
    const titleEl = document.getElementById('kanbanTaskModalTitleInput');
    if (titleEl) titleEl.focus();
  }, 50);
  document.addEventListener('keydown', _kanbanTaskModalKey);
}

async function openKanbanEdit(taskId){
  // Triggered by the Edit button on the task detail view.  Fetches the task
  // (rather than relying on whatever's cached locally) so the modal always
  // reflects authoritative server state.
  if (!taskId) return;
  if (typeof switchPanel === 'function' && _currentPanel !== 'kanban') switchPanel('kanban');
  const modal = document.getElementById('kanbanTaskModal');
  if (!modal) return;
  let task = null;
  try {
    const data = await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + _kanbanBoardQuery());
    task = data && data.task;
  } catch(e) {
    showToast((t('kanban_unavailable') || 'Kanban unavailable') + ': ' + (e.message || e), 'error');
    return;
  }
  if (!task) return;
  _kanbanTaskModalMode = 'edit';
  _kanbanTaskModalEditingId = task.id;
  // Track the displayed status so submitKanbanTaskModal can detect whether
  // the user actually picked a new value vs. the dropdown's mapped default.
  // Without this, editing a 'running'/'blocked'/'done'/'archived' task whose
  // real status maps to 'triage' for display would silently demote the task
  // (the mapped 'triage' would land in the PATCH payload, and _patch_task
  // would call _set_status_direct → reclaim worker → move to triage).
  const initialDisplayedStatus = _kanbanEditableStatusFor(task.status);
  const originalStatus = task.status || initialDisplayedStatus;
  _kanbanTaskModalInitialDisplayedStatus = initialDisplayedStatus;
  _kanbanResetTaskModalFields({
    title: task.title || '',
    body: task.body || '',
    status: initialDisplayedStatus,
    tenant: task.tenant || '',
    priority: typeof task.priority === 'number' ? task.priority : 0,
  });
  // Populate the assignee select AFTER reset so the option exists when we
  // call sel.value = currentAssignee.
  await _kanbanPopulateAssigneeSelect(task.assignee || '');
  _kanbanSetTaskModalStatusHint(originalStatus, initialDisplayedStatus);
  _kanbanSetTaskModalLabels('edit');
  _kanbanPopulateTenantDatalist();
  modal.hidden = false;
  if (_kanbanTaskModalFocusCleanup) {
    _kanbanTaskModalFocusCleanup();
    _kanbanTaskModalFocusCleanup = null;
  }
  _kanbanTaskModalFocusCleanup = _trapModalFocus(modal);
  setTimeout(() => {
    const titleEl = document.getElementById('kanbanTaskModalTitleInput');
    if (titleEl) { titleEl.focus(); titleEl.select(); }
  }, 50);
  document.addEventListener('keydown', _kanbanTaskModalKey);
}

function _kanbanEditableStatusFor(status){
  // The modal's status select only offers triage/todo/ready (the user-writable
  // states).  blocked/running/done/archived are reached via the detail-view
  // status buttons or the dispatcher.  Map non-editable states to a sensible
  // default so the user can still change them via the buttons after saving.
  const editable = new Set(['triage', 'todo', 'ready']);
  return editable.has(status) ? status : 'triage';
}

function _kanbanResetTaskModalFields(values){
  const v = values || {};
  const set = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.value = (val == null ? '' : String(val));
  };
  set('kanbanTaskModalTitleInput', v.title || '');
  set('kanbanTaskModalBody', v.body || '');
  set('kanbanTaskModalStatus', v.status || 'triage');
  // Assignee handled separately by _kanbanPopulateAssigneeSelect() because
  // it's a <select> populated from /api/profiles + board history; setting
  // .value before the options exist would silently fail.
  set('kanbanTaskModalTenant', v.tenant || '');
  set('kanbanTaskModalPriority', v.priority != null ? v.priority : 0);
  const errEl = document.getElementById('kanbanTaskModalError');
  if (errEl) { errEl.textContent = ''; delete errEl.dataset.warningShown; }
  const submitBtn = document.getElementById('kanbanTaskModalSubmit');
  if (submitBtn) submitBtn.disabled = false;
}

function _kanbanSetTaskModalLabels(mode){
  const titleH = document.getElementById('kanbanTaskModalTitle');
  const submitBtn = document.getElementById('kanbanTaskModalSubmit');
  if (mode === 'edit') {
    if (titleH) titleH.textContent = t('kanban_edit_task') || 'Edit task';
    if (submitBtn) submitBtn.textContent = t('save') || 'Save';
  } else {
    if (titleH) titleH.textContent = t('kanban_new_task') || 'New task';
    if (submitBtn) submitBtn.textContent = t('create') || 'Create';
  }
}

function _kanbanSetTaskModalStatusHint(realStatus, editableStatus){
  const hintEl = document.getElementById('kanbanTaskModalStatusOriginalHint');
  if (!hintEl) return;
  if (!realStatus || realStatus === editableStatus) {
    hintEl.hidden = true;
    hintEl.textContent = '';
    return;
  }
  const statusLabel = t(`kanban_status_${realStatus}`) || realStatus;
  hintEl.textContent = String(t('kanban_status_original_hint')).replace('{0}', statusLabel);
  hintEl.hidden = false;
}

function _kanbanPopulateTenantDatalist(){
  const tenants = (_kanbanBoard && Array.isArray(_kanbanBoard.tenants)) ? _kanbanBoard.tenants : [];
  const tList = document.getElementById('kanbanTaskModalTenantList');
  if (tList) tList.innerHTML = tenants.map(v => `<option value="${esc(v)}"></option>`).join('');
}

function _trapModalFocus(modalEl){
  if (!modalEl) return () => {};
  const selector = 'a[href], button, textarea, input, select, summary, [tabindex]:not([tabindex="-1"])';
  const collect = () => {
    const candidates = Array.from(modalEl.querySelectorAll(selector));
    return candidates.filter((el) => {
      if (el.disabled || el.hidden) return false;
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      return el.tabIndex >= 0;
    });
  };
  let focusableEls = collect();
  const onKeyDown = (ev) => {
    if (ev.key !== 'Tab') return;
    if (!focusableEls.length) {
      ev.preventDefault();
      return;
    }
    const current = document.activeElement;
    let idx = focusableEls.indexOf(current);
    if (idx === -1) {
      ev.preventDefault();
      focusableEls[0].focus();
      return;
    }
    if (ev.shiftKey) idx -= 1;
    else idx += 1;
    idx = (idx + focusableEls.length) % focusableEls.length;
    ev.preventDefault();
    focusableEls[idx].focus();
  };
  modalEl.addEventListener('keydown', onKeyDown);
  return () => {
    modalEl.removeEventListener('keydown', onKeyDown);
  };
}

function closeKanbanTaskModal(){
  const modal = document.getElementById('kanbanTaskModal');
  if (modal) modal.hidden = true;
  _kanbanTaskModalMode = 'create';
  _kanbanTaskModalEditingId = null;
  _kanbanTaskModalInitialDisplayedStatus = null;
  _kanbanSetTaskModalStatusHint(null, null);
  if (_kanbanTaskModalFocusCleanup) {
    _kanbanTaskModalFocusCleanup();
    _kanbanTaskModalFocusCleanup = null;
  }
  document.removeEventListener('keydown', _kanbanTaskModalKey);
}

function _kanbanTaskModalKey(ev){
  if (ev.key === 'Escape') {
    ev.preventDefault();
    closeKanbanTaskModal();
    return;
  }
  if (ev.key === 'Enter' && !ev.shiftKey) {
    // Enter submits except when the focus is in the description textarea
    // (where Enter should insert a newline).
    const target = ev.target;
    if (target && target.tagName === 'TEXTAREA') return;
    const modal = document.getElementById('kanbanTaskModal');
    if (modal && !modal.hidden) {
      ev.preventDefault();
      submitKanbanTaskModal();
    }
  }
}

async function submitKanbanTaskModal(){
  const titleEl = document.getElementById('kanbanTaskModalTitleInput');
  const bodyEl = document.getElementById('kanbanTaskModalBody');
  const statusEl = document.getElementById('kanbanTaskModalStatus');
  const assigneeEl = document.getElementById('kanbanTaskModalAssignee');
  const tenantEl = document.getElementById('kanbanTaskModalTenant');
  const priorityEl = document.getElementById('kanbanTaskModalPriority');
  const errEl = document.getElementById('kanbanTaskModalError');
  const submitBtn = document.getElementById('kanbanTaskModalSubmit');
  const title = titleEl ? titleEl.value.trim() : '';
  if (!title) {
    if (errEl) errEl.textContent = t('kanban_title_required') || 'Title is required.';
    if (titleEl) titleEl.focus();
    return;
  }
  // Build payload — for create we omit defaulted fields so the backend chooses;
  // for edit we send every field so users can clear assignee/tenant/body.
  const isEdit = _kanbanTaskModalMode === 'edit';
  const payload = {title};
  const bodyVal = bodyEl ? bodyEl.value : '';
  const assigneeVal = assigneeEl ? assigneeEl.value.trim() : '';
  const tenantVal = tenantEl ? tenantEl.value.trim() : '';
  const statusVal = statusEl ? statusEl.value : '';
  const priorityRaw = priorityEl ? priorityEl.value : '';
  if (isEdit) {
    payload.body = bodyVal;
    payload.assignee = assigneeVal || null;
    payload.tenant = tenantVal || null;
    // Only send status if the user actually changed the dropdown from the
    // value the modal opened with.  Otherwise editing a 'running'/'blocked'/
    // 'done'/'archived' task — whose real status maps to the dropdown's
    // 'triage' default — would silently demote the task on every save.
    if (statusVal && statusVal !== _kanbanTaskModalInitialDisplayedStatus) {
      payload.status = statusVal;
    }
    const n = parseInt(priorityRaw, 10);
    payload.priority = Number.isNaN(n) ? 0 : n;
  } else {
    if (bodyVal.trim()) payload.body = bodyVal;
    if (statusVal) payload.status = statusVal;
    if (assigneeVal) payload.assignee = assigneeVal;
    if (tenantVal) payload.tenant = tenantVal;
    if (priorityRaw !== '' && priorityRaw !== '0') {
      const n = parseInt(priorityRaw, 10);
      if (!Number.isNaN(n)) payload.priority = n;
    }
  }
  // Soft warning: a Ready task with the explicit "Unassigned" option will sit
  // forever because the dispatcher skips unassigned rows (kanban_db.py:3567).
  // The dropdown now makes this an explicit choice (the user picked "—
  // Unassigned (won't auto-run) —"), but we still surface a one-time confirm
  // so they don't lose work to a typo.
  if (statusVal === 'ready' && !assigneeVal) {
    if (errEl && !errEl.dataset.warningShown) {
      errEl.textContent = t('kanban_ready_needs_assignee')
        || 'You picked Unassigned + Ready. The dispatcher will skip this task. Submit again to confirm, or pick a profile.';
      errEl.dataset.warningShown = '1';
      const sel = document.getElementById('kanbanTaskModalAssignee');
      if (sel) sel.focus();
      return;
    }
  }
  if (submitBtn) submitBtn.disabled = true;
  if (errEl) { errEl.textContent = ''; delete errEl.dataset.warningShown; }
  try {
    let saved;
    if (isEdit && _kanbanTaskModalEditingId) {
      saved = await api(
        '/api/kanban/tasks/' + encodeURIComponent(_kanbanTaskModalEditingId) + _kanbanBoardQuery(),
        {method: 'PATCH', body: JSON.stringify(payload)},
      );
    } else {
      saved = await api('/api/kanban/tasks' + _kanbanBoardQuery(), {
        method: 'POST',
        body: JSON.stringify(payload),
      });
    }
    closeKanbanTaskModal();
    await loadKanban(true);
    const savedId = saved && saved.task && saved.task.id;
    if (savedId) {
      await loadKanbanTask(savedId);
    } else if (isEdit && _kanbanTaskModalEditingId) {
      await loadKanbanTask(_kanbanTaskModalEditingId);
    }
  } catch(e) {
    if (errEl) errEl.textContent = (e.message || String(e));
    if (submitBtn) submitBtn.disabled = false;
  }
}

async function updateKanbanTask(taskId, patch, opts){
  if (!taskId || !patch) return;
  try {
    const openDetail = !opts || opts.openDetail !== false;
    const updated = await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + _kanbanBoardQuery(), {
      method: 'PATCH',
      body: JSON.stringify(patch),
    });
    await loadKanban(true);
    if (openDetail) await loadKanbanTask((updated && updated.task && updated.task.id) || taskId);
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
      <button class="btn secondary kanban-edit-btn" onclick="openKanbanEdit('${esc(task.id)}')" data-i18n="kanban_edit_task" title="${esc(t('kanban_edit_task') || 'Edit task')}">${esc(t('kanban_edit_task') || 'Edit task')}</button>
    </div>
    <div class="kanban-task-preview-body">${_kanbanRenderMarkdown(body)}</div>
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
    try { data.log = await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + '/log' + _kanbanBoardQuery({tail: 65536})); } catch(e) { data.log = {}; }
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
  } else if (saved) {
    _kanbanSetSavedBoard('default');
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
  if (_kanbanBoardModalFocusCleanup) {
    _kanbanBoardModalFocusCleanup();
    _kanbanBoardModalFocusCleanup = null;
  }
  _kanbanBoardModalFocusCleanup = _trapModalFocus(modal);
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
  if (_kanbanBoardModalFocusCleanup) {
    _kanbanBoardModalFocusCleanup();
    _kanbanBoardModalFocusCleanup = null;
  }
  _kanbanBoardModalFocusCleanup = _trapModalFocus(modal);
  setTimeout(() => document.getElementById('kanbanBoardModalName').focus(), 50);
  document.addEventListener('keydown', _kanbanBoardModalEsc);
}

function _kanbanBoardModalEsc(ev){
  if (ev.key === 'Escape') closeKanbanBoardModal();
}

function closeKanbanBoardModal(){
  const modal = document.getElementById('kanbanBoardModal');
  if (modal) modal.hidden = true;
  if (_kanbanBoardModalFocusCleanup) {
    _kanbanBoardModalFocusCleanup();
    _kanbanBoardModalFocusCleanup = null;
  }
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

function _severityForLine(line) {
  const text = String(line || '').toUpperCase();
  if (/\b(ERROR|CRITICAL|TRACEBACK)\b/.test(text)) return 'error';
  if (/\b(WARNING|WARN)\b/.test(text)) return 'warning';
  if (/\b(DEBUG)\b/.test(text)) return 'debug';
  if (/\b(INFO)\b/.test(text)) return 'info';
  return 'other';
}

function _filteredLogsLines() {
  if (_logsSeverityFilter === 'all') return _lastLogsLines;
  return _lastLogsLines.filter(line => {
    const sev = _severityForLine(line);
    if (_logsSeverityFilter === 'errors') return sev === 'error';
    if (_logsSeverityFilter === 'warnings') return sev === 'warning' || sev === 'error';
    return true;
  });
}

function _applyLogsSeverityFilter() {
  const el = $('logsSeverityFilter');
  _logsSeverityFilter = (el && el.value) || 'all';
  // Re-render from cached lines without re-fetching
  _renderLogs({ lines: _lastLogsLines, hint: '', truncated: false, _fromFilter: true });
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
  const rawLines = Array.isArray(data && data.lines) ? data.lines : [];
  // Only update cache when loading fresh data (not when re-rendering from filter)
  if (data && !data._fromFilter) _lastLogsLines = rawLines.slice();
  const displayLines = _filteredLogsLines();
  const hint = data && data.hint ? `<div class="logs-hint">${esc(data.hint)}</div>` : '';
  const truncated = data && data.truncated ? `<div class="logs-hint warn">${esc(t('logs_truncated_hint'))}</div>` : '';
  const filterNote = _logsSeverityFilter !== 'all'
    ? `<div class="logs-hint">${esc(displayLines.length + ' / ' + _lastLogsLines.length + ' ' + t('logs_filter_active'))}</div>`
    : '';
  if (!displayLines.length) {
    box.innerHTML = `${hint}${truncated}${filterNote}<div class="logs-empty">${esc(t('logs_empty'))}</div>`;
  } else {
    box.innerHTML = `${hint}${truncated}${filterNote}` + displayLines.map(line => {
      const cls = _logLineSeverityClass(line);
      return `<div class="log-line ${cls}">${esc(line)}</div>`;
    }).join('');
  }
  _syncLogsWrap();
  if (status) {
    const bytes = data && Number(data.total_bytes || 0);
    const when = data && data.mtime ? new Date(data.mtime * 1000).toLocaleString() : t('logs_no_mtime');
    status.textContent = `${rawLines.length} / ${data.tail || _selectedLogsTail()} lines · ${bytes.toLocaleString()} bytes · ${when}`;
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
  const lines = _filteredLogsLines();
  const text = lines.join('\n');
  try {
    await _copyText(text);
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

/**
 * Bucket daily token rows for chart display.
 * Returns rows unchanged when length <= 30 (per-day resolution).
 * For longer ranges, groups consecutive days into buckets:
 *   31–90 days → 2-day buckets
 *   91–180 days → 3-day buckets
 *   181–365 days → 8-day buckets
 * Result is always <= ~52 bars.
 * Each bucket row has:
 *   - label: short label for axis (e.g. MM-DD or MM-DD–MM-DD)
 *   - title: full tooltip title (e.g. 2026-01-01 – 2026-01-05)
 *   - date: first date in bucket (used for date label slicing)
 *   - input_tokens, output_tokens, sessions, cost: summed across bucket
 */
function _bucketDailyTokensForChart(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return [];
  const len = rows.length;
  if (len <= 30) return rows;  // per-day resolution for 7/30-day ranges

  // Target <= 75 bars; derive bucket size
  let bucketSize;
  if (len <= 90) {
    bucketSize = 2;
  } else if (len <= 180) {
    bucketSize = 3;
  } else if (len <= 365) {
    bucketSize = 8;  // <=52 bars for 365 days (ceil(365/8)=46)
  } else {
    bucketSize = 8;  // fallback for >365 (shouldn't occur in practice)
  }

  const result = [];
  for (let i = 0; i < len; i += bucketSize) {
    const slice = rows.slice(i, i + bucketSize);
    const input_tokens = slice.reduce((s, r) => s + Number(r.input_tokens || 0), 0);
    const output_tokens = slice.reduce((s, r) => s + Number(r.output_tokens || 0), 0);
    const sessions = slice.reduce((s, r) => s + Number(r.sessions || 0), 0);
    const cost = slice.reduce((s, r) => s + Number(r.cost || 0), 0);

    const firstDate = slice[0].date;
    const lastDate = slice[slice.length - 1].date;

    // Label: short form for axis
    const firstLabel = String(firstDate).slice(5);  // MM-DD
    const lastLabel = String(lastDate).slice(5);
    const label = (firstDate === lastDate) ? firstLabel : (firstLabel + '–' + lastLabel);

    result.push({
      label,
      title: firstDate + (firstDate !== lastDate ? ' – ' + lastDate : ''),
      date: firstDate,
      input_tokens,
      output_tokens,
      sessions,
      cost,
    });
  }
  return result;
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

  // Daily token trend — bucket long ranges to avoid horizontal overflow
  const dailyTokens = Array.isArray(d.daily_tokens) ? d.daily_tokens : [];
  const chartRows = _bucketDailyTokensForChart(dailyTokens);
  let dailyHtml = '';
  if (chartRows.length) {
    const maxDailyTokens = Math.max(...chartRows.map(r => Number(r.input_tokens || 0) + Number(r.output_tokens || 0)), 1);
    const labelEvery = Math.max(Math.ceil(chartRows.length / 7), 1);
    dailyHtml = `<div class="insights-card"><div class="insights-card-title">${esc(t('insights_daily_tokens'))}</div><div class="insights-daily-token-chart">` +
      chartRows.map((r, idx) => {
        const input = Number(r.input_tokens || 0);
        const output = Number(r.output_tokens || 0);
        const inputPct = Math.max((input / maxDailyTokens) * 100, input ? 2 : 0).toFixed(1);
        const outputPct = Math.max((output / maxDailyTokens) * 100, output ? 2 : 0).toFixed(1);
        const showLabel = idx === 0 || idx === chartRows.length - 1 || idx % labelEvery === 0;
        const titleDate = r.title || r.date;
        const title = `${titleDate} · ${fmtTokens(input)} ${t('insights_input_tokens')} · ${fmtTokens(output)} ${t('insights_output_tokens')} · ${fmtCost(r.cost)} · ${fmtNum(r.sessions)} ${t('insights_sessions')}`;
        const labelText = r.label !== undefined ? r.label : String(r.date).slice(5);
        return `<div class="insights-daily-bar" title="${esc(title)}"><div class="insights-daily-stack" aria-label="${esc(title)}"><div class="insights-daily-bar-output" style="height:${outputPct}%"></div><div class="insights-daily-bar-input" style="height:${inputPct}%"></div></div><span>${showLabel ? esc(labelText) : ''}</span></div>`;
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
    <div class="insights-row insights-usage-grid">
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
      el.className = 'skill-item' + (skill.disabled ? ' disabled' : '');
      el.style.display = collapsed ? 'none' : '';
      const isDisabled = skill.disabled || false;
      const toggle = document.createElement('span');
      toggle.className = 'skill-toggle' + (isDisabled ? '' : ' enabled');
      toggle.title = isDisabled ? t('skill_disabled') : t('skill_enabled');
      toggle.addEventListener('click', (ev) => {
        ev.stopPropagation();
        toggleSkill(skill.name, !isDisabled);
      });
      const nameEl = document.createElement('span');
      nameEl.className = 'skill-name';
      nameEl.textContent = skill.name;
      const descEl = document.createElement('span');
      descEl.className = 'skill-desc';
      descEl.textContent = skill.description || '';
      el.append(toggle, nameEl, descEl);
      el.onclick = () => openSkill(skill.name, el);
      sec.appendChild(el);
    }
    box.appendChild(sec);
  }
}

function filterSkills() {
  if (_skillsData) renderSkills(_skillsData);
}


async function toggleSkill(name, currentlyEnabled) {
  const newEnabled = !currentlyEnabled;
  try {
    const result = await api('/api/skills/toggle', {
      method: 'POST',
      body: JSON.stringify({ name, enabled: newEnabled })
    });
    if (result && result.ok) {
      if (_skillsData) {
        const skill = _skillsData.find(s => s.name === name);
        if (skill) skill.disabled = !newEnabled;
      }
      renderSkills(_skillsData || []);
    } else {
      setStatus((result && result.error) || t('skill_toggle_failed'));
    }
  } catch(e) {
    setStatus(t('skill_toggle_failed') + e.message);
  }
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

function _renderSkillError(name, message) {
  const title = $('skillDetailTitle');
  const body = $('skillDetailBody');
  const empty = $('skillDetailEmpty');
  if (title) title.textContent = name;
  if (body) {
    body.innerHTML = `<div class="main-view-content"><div class="detail-form-error" style="display:block">${esc(message || t('skill_load_failed'))}</div></div>`;
    body.style.display = '';
  }
  if (empty) empty.style.display = 'none';
  _currentSkillDetail = null;
  _skillMode = 'empty';
  _setSkillHeaderButtons('empty');
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
    if (data && (data.success === false || data.error)) {
      const message = data.error || t('skill_load_failed');
      _renderSkillError(name, message);
      setStatus(t('skill_load_failed') + message);
      return;
    }
    _currentSkillDetail = { name, content: data.content || '', linked_files: data.linked_files || {} };
    _renderSkillDetail(name, data.content || '', data.linked_files || {});
  } catch(e) { setStatus(t('skill_load_failed') + e.message); }
}

async function openSkillFile(skillName, filePath) {
  try {
    const data = await api(`/api/skills/content?name=${encodeURIComponent(skillName)}&file=${encodeURIComponent(filePath)}`);
    if (data && data.error) {
      _renderSkillError(skillName, data.error);
      setStatus(t('skill_file_load_failed') + data.error);
      return;
    }
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
let _currentMemorySection = null; // 'memory' | 'user' | 'soul'
let _memoryMode = 'empty'; // 'empty' | 'read' | 'edit'

const MEMORY_SECTIONS = [
  { key: 'memory', labelKey: 'my_notes', emptyKey: 'no_notes_yet', iconKey: 'brain' },
  { key: 'user',   labelKey: 'user_profile', emptyKey: 'no_profile_yet', iconKey: 'user' },
  { key: 'soul',   labelKey: 'agent_soul', emptyKey: 'no_soul_yet', iconKey: 'sparkles' },
];

function _memorySectionMeta(key) {
  return MEMORY_SECTIONS.find(s => s.key === key) || MEMORY_SECTIONS[0];
}

function _memorySectionContent(key) {
  if (!_memoryData) return '';
  if (key === 'user') return _memoryData.user || '';
  if (key === 'soul') return _memoryData.soul || '';
  return _memoryData.memory || '';
}

function _memorySectionMtime(key) {
  if (!_memoryData) return 0;
  if (key === 'user') return _memoryData.user_mtime || 0;
  if (key === 'soul') return _memoryData.soul_mtime || 0;
  return _memoryData.memory_mtime || 0;
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

function renderWorkspaceDropdownInto(dd, workspaces, currentWs, opts){
  if(!dd)return;
  opts=opts||{};
  const onSelect=typeof opts.onSelect==='function'
    ? opts.onSelect
    : (path,name)=>switchToWorkspace(path,name);
  const includeSessionActions=opts.includeSessionActions!==false;
  const includeManageAction=opts.includeManageAction!==false;
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
      opt.onclick=()=>onSelect(w.path,w.name,w);
      listContainer.appendChild(opt);
    }
    listContainer.appendChild(noResults);
  }

  renderList();
  filterWs('');

  si.addEventListener('input',()=>{ filterWs(si.value); });
  sc.addEventListener('click',()=>{ si.value=''; filterWs(''); si.focus(); });

  // ── Footer actions ────────────────────────────────────────────────────────
  const appendDivider=()=>{const div=document.createElement('div');div.className='ws-divider';dd.appendChild(div);};
  if(includeSessionActions){
    appendDivider();
    dd.appendChild(_renderWorkspaceAction(
      t('workspace_new_worktree_conversation'),
      t('workspace_new_worktree_conversation_meta'),
      li('git-branch',12),
      async()=>{
        closeWsDropdown();
        try{
          await newSession(false,{worktree:true});
          await renderSessionList();
          const msg=$('msg');
          if(msg)msg.focus();
          showToast(t('workspace_worktree_created'));
        }catch(e){
          showToast(t('workspace_worktree_failed')+(e&&e.message?e.message:e),'error');
        }
      }
    ));
    appendDivider();
    dd.appendChild(_renderWorkspaceAction(
      t('workspace_choose_path'),
      t('workspace_choose_path_meta'),
      li('folder',12),
      ()=>promptWorkspacePath()
    ));
  }
  if(includeManageAction){
    appendDivider();
    dd.appendChild(_renderWorkspaceAction(
      t('workspace_manage'),
      t('workspace_manage_meta'),
      li('settings',12),
      ()=>{closeWsDropdown();mobileSwitchPanel('workspaces');}
    ));
  }
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
  const profileDd=$('profileDefaultWorkspaceDropdown');
  const composerChip=$('composerWorkspaceChip');
  const mobileAction=$('composerMobileWorkspaceAction');
  const profileChip=$('profileDefaultWorkspaceChip');
  if(dd)dd.classList.remove('open');
  if(composerDd)composerDd.classList.remove('open');
  if(profileDd)profileDd.classList.remove('open');
  if(composerChip)composerChip.classList.remove('active');
  if(mobileAction)mobileAction.classList.remove('active');
  if(profileChip)profileChip.classList.remove('active');
}
document.addEventListener('click',e=>{
  if(
    !e.target.closest('#composerWorkspaceChip') &&
    !e.target.closest('#composerMobileWorkspaceAction') &&
    !e.target.closest('#composerWsDropdown') &&
    !e.target.closest('#profileDefaultWorkspaceChip') &&
    !e.target.closest('#profileDefaultWorkspaceDropdown')
  ) closeWsDropdown();
});
window.addEventListener('resize',()=>{
  const dd=$('composerWsDropdown');
  if(dd&&dd.classList.contains('open')) _positionComposerWsDropdown();
  const profileDd=$('profileDefaultWorkspaceDropdown');
  const profileChip=$('profileDefaultWorkspaceChip');
  if(profileDd&&profileDd.classList.contains('open')) _positionProfileDefaultWorkspaceDropdown(profileChip, profileDd);
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
let _profileSwitchGeneration = 0;

async function _profileSwitchPanelLoad(){
  if (_currentPanel === 'skills') await loadSkills();
  if (_currentPanel === 'memory') await loadMemory();
  if (_currentPanel === 'tasks') await loadCrons();
  if (_currentPanel === 'kanban') await loadKanban();
  if (_currentPanel === 'profiles') await loadProfilesPanel();
  if (_currentPanel === 'workspaces') await loadWorkspacesPanel();
}

function _refreshProfileSwitchBackground(gen){
  window._modelDropdownReady=null;
  if (typeof window._ensureModelDropdownReady === 'function') {
    Promise.resolve(window._ensureModelDropdownReady()).catch(()=>{});
  }
  Promise.resolve(loadWorkspaceList()).then(()=>{
    if (gen !== _profileSwitchGeneration) return;
    if (S.session && typeof syncTopbar === 'function') syncTopbar();
  }).catch(()=>{});
  // Reconcile per-profile sidebar tab visibility. hidden_tabs is a per-profile
  // appearance setting; without this fetch, Profile A's hidden-tabs choice
  // would remain in effect under Profile B until the user opens Settings.
  // Stage-394 follow-up to #2636 deep review.
  Promise.resolve(api('/api/settings')).then(function(s){
    if (gen !== _profileSwitchGeneration) return;
    var hidden = (s && Array.isArray(s.hidden_tabs)) ? s.hidden_tabs : [];
    hidden = hidden.filter(function(x){ return typeof x === 'string' && x.trim(); });
    if (typeof _setHiddenTabs === 'function') _setHiddenTabs(hidden);
    if (typeof _applyTabVisibility === 'function') _applyTabVisibility(hidden);
  }).catch(function(){});
}

function _profileSkillsCountMeta(p){
  if (!p) return '';
  if (typeof p.skill_enabled_count !== 'number' || typeof p.skill_total !== 'number') return '';
  return `${p.skill_enabled_count} / ${p.skill_total} skills`;
}

function _syncProfileAvatarState(data){
  if(!data||!Array.isArray(data.profiles)||typeof setProfileAvatarMap!=='function') return;
  const active=(S.activeProfile&&data.profiles.some(p=>p.name===S.activeProfile))?S.activeProfile:(data.active||'default');
  setProfileAvatarMap(data.profiles, active);
}

function _profileAvatarForUi(profile, classes){
  const name=(profile&&profile.name)||'H';
  const fallback=name.charAt(0).toUpperCase()||'H';
  const shape=(typeof _normalizeProfileAvatarShape==='function')?_normalizeProfileAvatarShape(profile&&profile.avatar_shape):'circle';
  let avatar=profile&&profile.avatar;
  let state='';
  const mode=(typeof _normalizeProfileAvatarMode==='function')
    ? _normalizeProfileAvatarMode(profile&&profile.avatar_mode)
    : String(profile&&profile.avatar_mode||'static').toLowerCase();
  if(mode==='reactive'){
    const effective=profile&&profile.effective_reactive_avatar&&profile.effective_reactive_avatar.idle;
    if(effective&&effective.avatar){
      avatar=effective.avatar;
      state='idle';
    }else{
      const idleSlot=profile&&profile.reactive_avatar&&profile.reactive_avatar.slots&&profile.reactive_avatar.slots.idle;
      if(idleSlot&&idleSlot.url){
        avatar={type:'asset',value:idleSlot.url};
        state='idle';
      }
    }
  }
  if(typeof _profileAvatarMarkup==='function') return _profileAvatarMarkup(avatar,{fallback,shape,classes,title:name,state});
  return `<div class="${esc(classes||'profile-avatar')}">${esc(fallback)}</div>`;
}

function _profileModelGroupsFromComposer(){
  const sel=$('modelSelect');
  const groups=[];
  if(!sel) return groups;
  const pushGroup=(provider,label,options)=>{
    const models=options.map(opt=>({value:opt.value,label:opt.textContent||getModelLabel(opt.value),provider})).filter(m=>m.value);
    if(models.length) groups.push({provider:provider||'',label:label||provider||'Models',models});
  };
  for(const child of Array.from(sel.children)){
    if(child.tagName==='OPTGROUP'){
      pushGroup((child.dataset&&child.dataset.provider)||'', child.label||'', Array.from(child.children));
    }else if(child.tagName==='OPTION'){
      pushGroup('', 'Models', [child]);
    }
  }
  return groups;
}

function _profileProviderForModel(modelValue, groups){
  const explicit=(typeof _providerFromModelValue==='function')?_providerFromModelValue(modelValue):'';
  if(explicit) return explicit;
  for(const g of groups||[]){
    if((g.models||[]).some(m=>m.value===modelValue)) return g.provider||'';
  }
  return '';
}

// Legacy v2 identity-controls hydrator + its save path (_hydrateProfileIdentityControls,
// _saveProfileModelSettings, _profileMarkModelDirty, _profileSetInlineStatus) were removed
// on 2026-05-15 as part of the Runtime tile rework. The v3 layout never
// renders #profileInlineProvider/#profileInlineModel/#profileInlineReasoning, so the
// helpers had no live callers; the new _hydrateProfileDefaultModel + _persistProfileDefaultModel
// pair (further below) covers the same /api/profile/settings POST contract with auto-save.

let _profileAvatarUploadDataUrl='';
const PROFILE_AVATAR_UPLOAD_MAX_BYTES=3*1024*1024;
const PROFILE_REACTIVE_AVATAR_UPLOAD_MAX_BYTES=5*1024*1024;
const PROFILE_REACTIVE_AVATAR_SLOTS=[
  {key:'idle',label:'Idle',hint:'Waiting between turns'},
  {key:'thinking',label:'Thinking',hint:'Reasoning or planning'},
  {key:'talking',label:'Talking',hint:'Streaming text'},
  {key:'working',label:'Working',hint:'Tools, files, or web work'},
  {key:'error',label:'Error',hint:'Recoverable failure state'},
];
let _profileAvatarDialogSettings=null;
let _profileReactiveAvatarUploads={};
let _profileReactiveAvatarClearSlots=new Set();
let _profileAvatarDialogPreviewState='idle';

function _setProfileAvatarDialogMessage(message,tone){
  const el=$('profileAvatarDialogMessage');
  if(!el) return;
  const text=String(message||'').trim();
  el.textContent=text;
  el.hidden=!text;
  el.dataset.tone=tone||'';
}

function _profileAvatarRuntimeModeFromDialog(){
  const active=document.querySelector('[data-avatar-runtime-mode].active');
  const mode=(active&&active.dataset.avatarRuntimeMode)||'static';
  return mode==='reactive'?'reactive':'static';
}

function _setProfileAvatarRuntimeMode(mode){
  mode=mode==='reactive'?'reactive':'static';
  document.querySelectorAll('[data-avatar-runtime-mode]').forEach(btn=>{
    const active=btn.dataset.avatarRuntimeMode===mode;
    btn.classList.toggle('active',active);
    btn.setAttribute('aria-pressed',active?'true':'false');
  });
  const staticWrap=$('profileAvatarStaticEditor');
  const reactiveWrap=$('profileAvatarReactiveEditor');
  const previewStates=$('profileAvatarPreviewStateControls');
  if(staticWrap) staticWrap.hidden=mode!=='static';
  if(reactiveWrap) reactiveWrap.hidden=mode!=='reactive';
  if(previewStates) previewStates.hidden=mode!=='reactive';
  _refreshProfileAvatarDialogPreview();
}

function _profileAvatarUploadTypeAllowed(file){
  if(!file) return false;
  const type=String(file.type||'').toLowerCase();
  const name=String(file.name||'').toLowerCase();
  return /^image\/(png|jpe?g|gif|webp)$/.test(type)||/\.(png|jpe?g|gif|webp)$/.test(name);
}

function _profileReactiveSlotMeta(slot){
  if(_profileReactiveAvatarClearSlots.has(slot)) return null;
  const upload=_profileReactiveAvatarUploads[slot];
  if(upload&&upload.previewUrl) return {url:upload.previewUrl,filename:upload.file&&upload.file.name,uploaded:true};
  const settings=_profileAvatarDialogSettings||{};
  const slots=settings.reactive_avatar&&settings.reactive_avatar.slots||{};
  return slots&&slots[slot]||null;
}

function _profileReactiveAvatarForSlot(slot){
  const meta=_profileReactiveSlotMeta(slot);
  return meta&&meta.url?{type:'asset',value:meta.url}:null;
}

function _normalizeProfileAvatarPreviewState(state){
  const normalized=String(state||'idle').trim().toLowerCase();
  return PROFILE_REACTIVE_AVATAR_SLOTS.some(slot=>slot.key===normalized)?normalized:'idle';
}

function _profileReactivePreviewSlot(desiredState){
  desiredState=_normalizeProfileAvatarPreviewState(desiredState);
  const orderedSlots=[desiredState];
  if(desiredState!=='idle') orderedSlots.push('idle');
  PROFILE_REACTIVE_AVATAR_SLOTS.forEach(slot=>{
    if(!orderedSlots.includes(slot.key)) orderedSlots.push(slot.key);
  });
  for(const slot of orderedSlots){
    const avatar=_profileReactiveAvatarForSlot(slot);
    if(avatar) return slot;
  }
  return null;
}

function _profileReactivePreviewAvatar(desiredState){
  desiredState=_normalizeProfileAvatarPreviewState(desiredState);
  const orderedSlots=[desiredState];
  if(desiredState!=='idle') orderedSlots.push('idle');
  PROFILE_REACTIVE_AVATAR_SLOTS.forEach(slot=>{
    if(!orderedSlots.includes(slot.key)) orderedSlots.push(slot.key);
  });
  for(const slot of orderedSlots){
    const avatar=_profileReactiveAvatarForSlot(slot);
    if(avatar) return avatar;
  }
  if(_profileAvatarDialogSettings&&_profileAvatarDialogSettings.avatar) return _profileAvatarDialogSettings.avatar;
  try{return _profileAvatarRuntimeModeFromDialog()==='static'?_profileAvatarPayloadFromDialog():null;}catch(_){return null;}
}

function _setProfileAvatarPreviewState(state){
  _profileAvatarDialogPreviewState=_normalizeProfileAvatarPreviewState(state);
  document.querySelectorAll('[data-avatar-preview-state]').forEach(btn=>{
    const active=btn.dataset.avatarPreviewState===_profileAvatarDialogPreviewState;
    btn.classList.toggle('active',active);
    btn.setAttribute('aria-pressed',active?'true':'false');
  });
  _refreshProfileAvatarDialogPreview();
}

function _refreshReactiveAvatarSlotRows(){
  PROFILE_REACTIVE_AVATAR_SLOTS.forEach(slotInfo=>{
    const slot=slotInfo.key;
    const meta=_profileReactiveSlotMeta(slot);
    const preview=$(`profileReactiveAvatar${slot}Preview`);
    const status=$(`profileReactiveAvatar${slot}Status`);
    if(preview){
      const avatar=meta&&meta.url?{type:'asset',value:meta.url}:null;
      preview.innerHTML=avatar&&typeof _profileAvatarMarkup==='function'
        ? _profileAvatarMarkup(avatar,{fallback:slotInfo.label.charAt(0),shape:_profileAvatarShapeFromDialog(),classes:'profile-avatar--slot',title:slotInfo.label})
        : `<div class="profile-avatar profile-avatar--slot profile-avatar-shape--${esc(_profileAvatarShapeFromDialog())} profile-avatar--fallback">${esc(slotInfo.label.charAt(0))}</div>`;
    }
    if(status){
      if(_profileReactiveAvatarUploads[slot]) status.textContent=`Selected: ${_profileReactiveAvatarUploads[slot].file.name}`;
      else if(_profileReactiveAvatarClearSlots.has(slot)) status.textContent='Will clear on save';
      else if(meta&&meta.filename) status.textContent=`Saved: ${meta.filename}`;
      else status.textContent='Using fallback';
    }
  });
}

function _revokeReactiveAvatarUploadUrls(){
  Object.values(_profileReactiveAvatarUploads||{}).forEach(upload=>{
    if(upload&&upload.previewUrl){
      try{URL.revokeObjectURL(upload.previewUrl);}catch(_){}
    }
  });
}

function _chooseProfileAvatarEmoji(value){
  const input=$('profileAvatarEmoji');
  if(input){
    input.value=value;
    _setProfileAvatarDialogMode('emoji');
    _setProfileAvatarDialogMessage('', '');
    _refreshProfileAvatarDialogPreview();
    input.focus();
  }
}

function _profileAvatarDialogModeFromAvatar(avatar){
  const normalized=(typeof _normalizeProfileAvatar==='function')?_normalizeProfileAvatar(avatar):null;
  if(!normalized) return 'emoji';
  if(normalized.type==='image') return 'upload';
  return normalized.type||'emoji';
}

function _setProfileAvatarDialogMode(mode){
  mode=['emoji','url','upload','asset'].includes(mode)?mode:'emoji';
  document.querySelectorAll('[data-avatar-mode]').forEach(btn=>{
    const active=btn.dataset.avatarMode===mode;
    btn.classList.toggle('active',active);
    btn.setAttribute('aria-pressed',active?'true':'false');
  });
  document.querySelectorAll('[data-profile-avatar-pane]').forEach(pane=>{
    pane.hidden=pane.dataset.profileAvatarPane!==mode;
  });
  _refreshProfileAvatarDialogPreview();
}

function _profileAvatarShapeFromDialog(){
  const active=document.querySelector('[data-avatar-shape].active');
  const shape=(active&&active.dataset.avatarShape)||'circle';
  return (typeof _normalizeProfileAvatarShape==='function')?_normalizeProfileAvatarShape(shape):shape;
}

function _setProfileAvatarDialogShape(shape){
  const normalized=(typeof _normalizeProfileAvatarShape==='function')?_normalizeProfileAvatarShape(shape):'circle';
  document.querySelectorAll('[data-avatar-shape]').forEach(btn=>{
    const active=btn.dataset.avatarShape===normalized;
    btn.classList.toggle('active',active);
    btn.setAttribute('aria-pressed',active?'true':'false');
  });
  _refreshReactiveAvatarSlotRows();
  _refreshProfileAvatarDialogPreview();
}

function _profileAvatarPreviewMarkup(profileName,avatar,shape,state){
  const fallback=String(profileName||'H').trim().slice(0,2).toUpperCase()||'H';
  if(typeof _profileAvatarMarkup==='function'){
    return _profileAvatarMarkup(avatar,{fallback,shape,classes:'profile-avatar--dialog',title:profileName,state});
  }
  return _profileAvatarForUi({name:profileName,avatar,avatar_shape:shape},'profile-avatar--dialog');
}

function _profileAvatarPayloadFromDialog(){
  const active=document.querySelector('[data-avatar-mode].active');
  const mode=(active&&active.dataset.avatarMode)||'emoji';
  if(mode==='emoji'){
    const value=($('profileAvatarEmoji')&&$('profileAvatarEmoji').value.trim())||'';
    if(!value) throw new Error('Choose an emoji avatar or clear the avatar.');
    return {type:'emoji',value};
  }
  if(mode==='url'){
    const value=($('profileAvatarUrl')&&$('profileAvatarUrl').value.trim())||'';
    if(!value) throw new Error('Enter an image or GIF URL.');
    return {type:'url',value};
  }
  if(mode==='upload'){
    if(!_profileAvatarUploadDataUrl) throw new Error('Choose an image, GIF, or WebP file.');
    return {type:'image',value:_profileAvatarUploadDataUrl};
  }
  const value=($('profileAvatarAsset')&&$('profileAvatarAsset').value.trim())||'';
  if(!value) throw new Error('Enter a safe asset reference.');
  return {type:'asset',value};
}

function _refreshProfileAvatarDialogPreview(){
  const node=$('profileAvatarDialogPreview');
  if(!node) return;
  const profileName=node.dataset.profileName||(_currentProfileDetail&&_currentProfileDetail.name)||'H';
  const runtimeMode=_profileAvatarRuntimeModeFromDialog();
  const previewState=_normalizeProfileAvatarPreviewState(_profileAvatarDialogPreviewState);
  const summary=$('profileAvatarPreviewSummary');
  try{
    const avatar=runtimeMode==='reactive'
      ? _profileReactivePreviewAvatar(previewState)
      : _profileAvatarPayloadFromDialog();
    if(!avatar&&runtimeMode!=='reactive') throw new Error('No avatar preview');
    node.innerHTML=_profileAvatarPreviewMarkup(profileName,avatar,_profileAvatarShapeFromDialog(),runtimeMode==='reactive'?previewState:'');
    node.dataset.previewState=runtimeMode==='reactive'?previewState:'static';
    if(summary){
      if(runtimeMode==='reactive'){
        const desired=PROFILE_REACTIVE_AVATAR_SLOTS.find(slot=>slot.key===previewState);
        const sourceSlot=_profileReactivePreviewSlot(previewState);
        const source=sourceSlot&&PROFILE_REACTIVE_AVATAR_SLOTS.find(slot=>slot.key===sourceSlot);
        if(sourceSlot&&sourceSlot!==previewState) summary.textContent=`${desired?desired.label:'Selected'} is falling back to ${source?source.label.toLowerCase():sourceSlot}.`;
        else if(sourceSlot) summary.textContent=`Previewing the ${source?source.label.toLowerCase():sourceSlot} animation slot.`;
        else summary.textContent='No animation slot is saved yet; the profile fallback remains available.';
      }else{
        summary.textContent='Static avatar preview. Reactive uploads stay saved when you switch modes.';
      }
    }
  }catch(_){
    const current=_currentProfileDetail&&_currentProfileDetail.name===profileName?_currentProfileDetail:null;
    const shape=_profileAvatarShapeFromDialog();
    node.innerHTML=_profileAvatarForUi(Object.assign({}, current||{name:profileName}, {avatar_shape:shape}),'profile-avatar--dialog');
    node.dataset.previewState=runtimeMode==='reactive'?previewState:'static';
    if(summary) summary.textContent='Preview is using the current profile avatar until the selected input is valid.';
  }
  _refreshReactiveAvatarSlotRows();
}

function _openProfileAvatarDialog(profileName){
  const dialog=$('profileAvatarDialog');
  if(!dialog) return;
  const profile=_profilesCache&&Array.isArray(_profilesCache.profiles)?_profilesCache.profiles.find(p=>p.name===profileName):_currentProfileDetail;
  const avatar=(profile&&profile.avatar)||null;
  const normalized=(typeof _normalizeProfileAvatar==='function')?_normalizeProfileAvatar(avatar):null;
  const shape=(profile&&profile.avatar_shape)||'circle';
  _revokeReactiveAvatarUploadUrls();
  _profileReactiveAvatarUploads={};
  _profileReactiveAvatarClearSlots=new Set();
  _profileAvatarDialogPreviewState='idle';
  _profileAvatarDialogSettings={
    name:profileName,
    avatar,
    avatar_shape:shape,
    avatar_mode:(profile&&profile.avatar_mode)||'static',
    reactive_avatar:(profile&&profile.reactive_avatar)||{version:1,updated_at:null,slots:{}},
    effective_reactive_avatar:(profile&&profile.effective_reactive_avatar)||{},
  };
  _profileAvatarUploadDataUrl=normalized&&normalized.type==='image'?normalized.value:'';
  if($('profileAvatarEmoji')) $('profileAvatarEmoji').value=normalized&&normalized.type==='emoji'?normalized.value:'🤖';
  if($('profileAvatarUrl')) $('profileAvatarUrl').value=normalized&&normalized.type==='url'?normalized.value:'';
  if($('profileAvatarAsset')) $('profileAvatarAsset').value=normalized&&normalized.type==='asset'?normalized.value:'';
  _setProfileAvatarDialogMessage('', '');
  const preview=$('profileAvatarDialogPreview');
  if(preview) preview.dataset.profileName=profileName;
  dialog.hidden=false;
  _setProfileAvatarDialogShape(shape);
  _setProfileAvatarDialogMode(_profileAvatarDialogModeFromAvatar(avatar));
  _setProfileAvatarPreviewState('idle');
  _setProfileAvatarRuntimeMode(_profileAvatarDialogSettings.avatar_mode);
  _refreshReactiveAvatarSlotRows();
  const file=$('profileAvatarUpload');
  if(file) file.value='';
  PROFILE_REACTIVE_AVATAR_SLOTS.forEach(slot=>{const input=$(`profileReactiveAvatar${slot.key}`);if(input) input.value='';});
  api(`/api/profile/avatar-settings?name=${encodeURIComponent(profileName)}`).then(settings=>{
    if(!settings||$('profileAvatarDialog')?.hidden) return;
    const currentName=$('profileAvatarDialogPreview')&&$('profileAvatarDialogPreview').dataset.profileName;
    if(currentName!==profileName) return;
    _profileAvatarDialogSettings=settings;
    const freshAvatar=settings.avatar||null;
    const freshNormalized=(typeof _normalizeProfileAvatar==='function')?_normalizeProfileAvatar(freshAvatar):null;
    _profileAvatarUploadDataUrl=freshNormalized&&freshNormalized.type==='image'?freshNormalized.value:'';
    if($('profileAvatarEmoji')) $('profileAvatarEmoji').value=freshNormalized&&freshNormalized.type==='emoji'?freshNormalized.value:'🤖';
    if($('profileAvatarUrl')) $('profileAvatarUrl').value=freshNormalized&&freshNormalized.type==='url'?freshNormalized.value:'';
    if($('profileAvatarAsset')) $('profileAvatarAsset').value=freshNormalized&&freshNormalized.type==='asset'?freshNormalized.value:'';
    _setProfileAvatarDialogShape(settings.avatar_shape||shape);
    _setProfileAvatarDialogMode(_profileAvatarDialogModeFromAvatar(freshAvatar));
    _setProfileAvatarRuntimeMode(settings.avatar_mode||'static');
    _refreshReactiveAvatarSlotRows();
  }).catch(e=>{
    _setProfileAvatarDialogMessage('Avatar settings load failed: '+(e.message||String(e)),'error');
  });
}

function _closeProfileAvatarDialog(){
  const dialog=$('profileAvatarDialog');
  if(dialog) dialog.hidden=true;
  _profileAvatarUploadDataUrl='';
  _profileAvatarDialogSettings=null;
  _profileReactiveAvatarClearSlots=new Set();
  _revokeReactiveAvatarUploadUrls();
  _profileReactiveAvatarUploads={};
}

function _readProfileAvatarFile(file){
  return new Promise((resolve,reject)=>{
    if(!file) return reject(new Error('Choose an image, GIF, or WebP file.'));
    if(!_profileAvatarUploadTypeAllowed(file)) return reject(new Error('Avatar upload must be PNG, JPEG, GIF, or WebP.'));
    if(file.size>PROFILE_AVATAR_UPLOAD_MAX_BYTES) return reject(new Error('Avatar upload must be 3 MB or smaller.'));
    const reader=new FileReader();
    reader.onload=()=>resolve(String(reader.result||''));
    reader.onerror=()=>reject(new Error('Could not read avatar file.'));
    reader.readAsDataURL(file);
  });
}

function _handleProfileAvatarUpload(file){
  _setProfileAvatarDialogMessage('Reading image…','info');
  return _readProfileAvatarFile(file).then(v=>{
    _profileAvatarUploadDataUrl=v;
    _refreshProfileAvatarDialogPreview();
    _setProfileAvatarDialogMessage('Image ready. Save avatar to apply it.','success');
  }).catch(e=>{
    _profileAvatarUploadDataUrl='';
    _refreshProfileAvatarDialogPreview();
    _setProfileAvatarDialogMessage(e.message||String(e),'error');
    if(typeof showToast==='function') showToast(e.message||String(e),4000,'error');
  });
}

function _handleReactiveAvatarUpload(slot,file){
  const slotInfo=PROFILE_REACTIVE_AVATAR_SLOTS.find(s=>s.key===slot);
  if(!slotInfo) return;
  try{
    if(!file) throw new Error('Choose an image, GIF, or WebP file.');
    if(!_profileAvatarUploadTypeAllowed(file)) throw new Error('Reactive avatars must be PNG, JPEG, GIF, or WebP.');
    if(file.size>PROFILE_REACTIVE_AVATAR_UPLOAD_MAX_BYTES) throw new Error('Reactive avatar uploads must be 5 MB or smaller.');
    const prior=_profileReactiveAvatarUploads[slot];
    if(prior&&prior.previewUrl) try{URL.revokeObjectURL(prior.previewUrl);}catch(_){}
    _profileReactiveAvatarUploads[slot]={file,previewUrl:URL.createObjectURL(file)};
    _profileReactiveAvatarClearSlots.delete(slot);
    _setProfileAvatarDialogMessage(`${slotInfo.label} animation selected. Save avatar to apply it.`,'success');
    _refreshProfileAvatarDialogPreview();
  }catch(e){
    delete _profileReactiveAvatarUploads[slot];
    _setProfileAvatarDialogMessage(e.message||String(e),'error');
    if(typeof showToast==='function') showToast(e.message||String(e),4000,'error');
    _refreshProfileAvatarDialogPreview();
  }
}

function _clearReactiveAvatarSlot(slot){
  if(!PROFILE_REACTIVE_AVATAR_SLOTS.some(s=>s.key===slot)) return;
  const prior=_profileReactiveAvatarUploads[slot];
  if(prior&&prior.previewUrl) try{URL.revokeObjectURL(prior.previewUrl);}catch(_){}
  delete _profileReactiveAvatarUploads[slot];
  _profileReactiveAvatarClearSlots.add(slot);
  const input=$(`profileReactiveAvatar${slot}`);
  if(input) input.value='';
  _setProfileAvatarDialogMessage('Slot will be cleared when you save.','info');
  _refreshProfileAvatarDialogPreview();
}

async function _postProfileAvatarSettings(form){
  const url=new URL('api/profile/avatar-settings',document.baseURI||location.href);
  const res=await fetch(url.href,{method:'POST',body:form,credentials:'include'});
  const text=await res.text();
  let data=null;
  try{data=text?JSON.parse(text):null;}catch(_){}
  if(!res.ok){
    const err=new Error((data&&(data.error||data.message))||text||res.statusText||'Avatar save failed');
    err.status=res.status;
    throw err;
  }
  return data||{};
}

function _mergeProfileAvatarSettings(profileName, updated){
  const apply=p=>{
    if(!p) return;
    p.avatar=updated.avatar||null;
    p.avatar_shape=updated.avatar_shape||'circle';
    p.avatar_mode=updated.avatar_mode||'static';
    p.reactive_avatar=updated.reactive_avatar||{version:1,updated_at:null,slots:{}};
    p.effective_reactive_avatar=updated.effective_reactive_avatar||{};
  };
  const p=_profilesCache&&Array.isArray(_profilesCache.profiles)?_profilesCache.profiles.find(x=>x.name===profileName):null;
  apply(p);
  if(_currentProfileDetail&&_currentProfileDetail.name===profileName) apply(_currentProfileDetail);
}

function _replaceProfileAvatarElement(node, profile, classes){
  if(!node||!profile) return;
  const wrap=document.createElement('div');
  wrap.innerHTML=_profileAvatarForUi(profile,classes);
  const next=wrap.firstElementChild;
  if(next) node.replaceWith(next);
}

function _profileForAvatarSurfaceRefresh(profileName){
  if(_profilesCache&&Array.isArray(_profilesCache.profiles)){
    const cached=_profilesCache.profiles.find(x=>x.name===profileName);
    if(cached) return cached;
  }
  if(_currentProfileDetail&&_currentProfileDetail.name===profileName) return _currentProfileDetail;
  return null;
}

function _refreshProfileAvatarSurfaces(profileName){
  const profile=_profileForAvatarSurfaceRefresh(profileName);
  if(!profile) return;
  const selectorName=CSS.escape(profileName);
  const card=document.querySelector(`.profile-card[data-name="${selectorName}"]`);
  if(card){
    _replaceProfileAvatarElement(card.querySelector('.profile-card-header .profile-avatar'),profile,'profile-avatar--card');
  }
  if(_currentProfileDetail&&_currentProfileDetail.name===profileName){
    const hero=$('profileHeroAvatar');
    if(hero){
      const shape=(typeof _normalizeProfileAvatarShape==='function')?_normalizeProfileAvatarShape(profile.avatar_shape):'circle';
      hero.classList.remove('profile-avatar-shape--square','profile-avatar-shape--circle');
      hero.classList.add(`profile-avatar-shape--${shape}`);
      const heroAvatar=Array.from(hero.children).find(el=>el.classList&&el.classList.contains('profile-avatar'));
      _replaceProfileAvatarElement(heroAvatar,profile,'profile-avatar--hero');
    }
  }
  const dropdown=$('profileDropdown');
  const option=dropdown&&dropdown.querySelector(`.profile-opt[data-name="${selectorName}"]`);
  if(option){
    _replaceProfileAvatarElement(option.querySelector('.profile-opt-main .profile-avatar'),profile,'profile-avatar--dropdown');
  }
}

async function refreshActiveProfileAvatarSettings(profileName){
  if(!profileName) profileName=S.activeProfile||'default';
  try{
    const settings=await api(`/api/profile/avatar-settings?name=${encodeURIComponent(profileName)}`);
    if(typeof setProfileAvatarEntry==='function') setProfileAvatarEntry(profileName,settings);
    if((S.activeProfile||'default')===profileName&&typeof setActiveProfileAvatarSettings==='function'){
      setActiveProfileAvatarSettings(settings);
    }
    _mergeProfileAvatarSettings(profileName,settings);
    return settings;
  }catch(_){
    return null;
  }
}

async function _saveProfileAvatar(profileName, clear, opts={}){
  const btn=$('profileAvatarSave');
  if(btn){btn.disabled=true;btn.style.opacity='0.55';}
  try{
    const form=new FormData();
    const avatar_shape=_profileAvatarShapeFromDialog();
    const runtimeMode=_profileAvatarRuntimeModeFromDialog();
    form.append('name',profileName);
    form.append('avatar_shape',avatar_shape);
    form.append('avatar_mode',clear?'static':runtimeMode);
    if(clear){
      form.append('avatar','null');
    }else if(runtimeMode==='static'){
      form.append('avatar',JSON.stringify(_profileAvatarPayloadFromDialog()));
    }
    if(opts.clearReactive) form.append('clear_reactive_avatar','true');
    else if(_profileReactiveAvatarClearSlots.size) form.append('clear_slots',Array.from(_profileReactiveAvatarClearSlots).join(','));
    Object.entries(_profileReactiveAvatarUploads).forEach(([slot,upload])=>{
      if(upload&&upload.file) form.append(`slot_${slot}`,upload.file,upload.file.name);
    });
    _setProfileAvatarDialogMessage(clear?'Clearing static avatar…':opts.clearReactive?'Clearing animations…':'Saving avatar…','info');
    const updated=await _postProfileAvatarSettings(form);
    _mergeProfileAvatarSettings(profileName,updated);
    if(_profilesCache) _syncProfileAvatarState(_profilesCache);
    if(typeof setProfileAvatarEntry==='function') setProfileAvatarEntry(profileName,updated);
    const isActive=(S.activeProfile||(_profilesCache&&_profilesCache.active)||'default')===profileName;
    if(isActive&&typeof setActiveProfileAvatarSettings==='function') setActiveProfileAvatarSettings(updated);
    if(typeof currentSessionProfile==='function'&&currentSessionProfile()===profileName&&typeof refreshAssistantProfileAvatars==='function'){
      refreshAssistantProfileAvatars({state:window._activeProfileAvatarState||'idle',force:true});
    }
    _closeProfileAvatarDialog();
    _refreshProfileAvatarSurfaces(profileName);
    showToast(clear?'Profile static avatar cleared':opts.clearReactive?'Profile avatar animations cleared':'Profile avatar saved');
  }catch(e){
    const message='Profile avatar save failed: '+(e.message||String(e));
    _setProfileAvatarDialogMessage(message,'error');
    showToast(message,4000,'error');
  }finally{
    if(btn){btn.disabled=false;btn.style.opacity='';}
  }
}

async function loadProfilesPanel() {
  const panel = $('profilesPanel');
  if (!panel) return;
  try {
    const data = await api('/api/profiles');
    _profilesCache = data;
    panel.innerHTML = '';
    const explainer = document.createElement('div');
    explainer.className = 'profile-card profile-help-card';
    explainer.innerHTML = `
      <div class="profile-card-header">
        <div style="min-width:0;flex:1">
          <div class="profile-card-name">Profiles vs workspaces</div>
          <div class="profile-card-meta">Use profiles for how the agent works; use workspaces for what files it works on.</div>
        </div>
      </div>`;
    explainer.onclick = () => _renderProfileConceptHelp(data.active || 'default');
    panel.appendChild(explainer);
    if (!data.profiles || !data.profiles.length) {
      const emptyMsg = document.createElement('div');
      emptyMsg.style.cssText = 'padding:16px;color:var(--muted);font-size:12px';
      emptyMsg.textContent = t('profiles_no_profiles');
      panel.appendChild(emptyMsg);
      if (_profileMode !== 'create') _clearProfileDetail();
      return;
    }
    const activeName = (S.activeProfile && data.profiles.some(p => p.name === S.activeProfile))
      ? S.activeProfile
      : (data.active || 'default');
    _syncProfileAvatarState(data);
    for (const p of data.profiles) {
      const card = document.createElement('div');
      card.className = 'profile-card';
      card.dataset.name = p.name;
      card.setAttribute('role', 'button');
      card.tabIndex = 0;
      const meta = [];
      if (p.model) meta.push(p.model.split('/').pop());
      if (p.provider) meta.push(p.provider);
      const skillsMeta = _profileSkillsCountMeta(p);
      if (skillsMeta) meta.push(skillsMeta);
      const gatewayState = _profileCardGatewayPhase(p);
      const gatewayLabel = _profileCardGatewayLabel(gatewayState);
      const gatewaySignal = `<span class="profile-card-gateway profile-wifi" data-profile-name="${esc(p.name)}" data-state="${esc(gatewayState)}" aria-hidden="true" title="${esc(gatewayLabel)}">${li('wifi',15)}</span><span class="sr-only">${esc(gatewayLabel)}</span>`;
      const isActive = p.name === activeName;
      const activeBadge = isActive ? `<span class="profile-card-active-pill">${esc(t('profile_active'))}</span>` : '';
      const ariaLabelParts = [p.name];
      if (isActive) ariaLabelParts.push('(active)');
      ariaLabelParts.push(gatewayLabel);
      card.setAttribute('aria-label', ariaLabelParts.join(' '));
      if (isActive) card.setAttribute('aria-current', 'true');
      card.innerHTML = `
        <div class="profile-card-header">
          ${_profileAvatarForUi(p,'profile-avatar--card')}
          <div class="profile-card-copy">
            <div class="profile-card-name${isActive ? ' is-active' : ''}"><span class="profile-card-name-text">${esc(p.name)}</span>${activeBadge}</div>
            ${meta.length ? `<div class="profile-card-meta">${esc(meta.join(' \u00b7 '))}</div>` : `<div class="profile-card-meta">${esc(t('profile_no_configuration'))}</div>`}
          </div>
          <div class="profile-card-actions">${gatewaySignal}<button type="button" class="profile-card-chat-btn" aria-label="Start chat with ${esc(p.name)}" title="Start chat">${li('message-square',15)}</button></div>
        </div>`;
      card.onclick = (ev) => {
        if (ev.target && ev.target.closest && ev.target.closest('.profile-card-chat-btn')) return;
        openProfileDetail(p.name, card);
        if (typeof closeMobileSidebar === 'function') closeMobileSidebar();
      };
      card.onkeydown = (ev) => {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        if (ev.target && ev.target.closest && ev.target.closest('.profile-card-chat-btn')) return;
        ev.preventDefault();
        openProfileDetail(p.name, card);
        if (typeof closeMobileSidebar === 'function') closeMobileSidebar();
      };
      const chatBtn = card.querySelector('.profile-card-chat-btn');
      if (chatBtn) chatBtn.onclick = (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        startChatWithProfile(p.name);
      };
      if (_currentProfileDetail && _currentProfileDetail.name === p.name) card.classList.add('active');
      panel.appendChild(card);
    }
    // Re-render detail with fresh data if we have one and we're not in a form.
    // If no detail is selected, auto-select the active/default so the detail
    // pane is never blank when profiles exist.
    if (_currentProfileDetail && _profileMode !== 'create') {
      const refreshed = data.profiles.find(p => p.name === _currentProfileDetail.name);
      if (refreshed) _renderProfileDetail(refreshed, data.active);
      else _clearProfileDetail();
    } else if (!_currentProfileDetail && _profileMode !== 'create') {
      const auto = data.profiles.find(p => p.name === activeName) || data.profiles[0];
      if (auto) {
        const targetCard = panel.querySelector(`.profile-card[data-name="${CSS.escape(auto.name)}"]`);
        if (targetCard) targetCard.classList.add('active');
        _renderProfileDetail(auto, data.active);
      }
    }
  } catch (e) {
    panel.innerHTML = `<div style="color:var(--accent);font-size:12px;padding:12px">${esc(t('error_prefix'))}${esc(e.message)}</div>`;
  }
}

function _renderProfileConceptHelp(activeName){
  const title = $('profileDetailTitle');
  const body = $('profileDetailBody');
  const empty = $('profileDetailEmpty');
  if (!title || !body) return;
  title.textContent = 'Profiles vs workspaces';
  body.innerHTML = `
    <div class="main-view-content">
      <div class="detail-card">
        <div class="detail-card-title">Use profiles for how; workspaces for what</div>
        <div class="detail-row"><div class="detail-row-label">Profiles</div><div class="detail-row-value">Agent identity, memory, skills, model/provider config, and connected tools. Create profiles for roles like researcher, writer, marketer, or developer when those roles should carry different context or capabilities.</div></div>
        <div class="detail-row"><div class="detail-row-label">Workspaces</div><div class="detail-row-value">Project or product folders on disk. Use one workspace per repo/product so chat, terminal, and file browsing point at the right files.</div></div>
        <div class="detail-row"><div class="detail-row-label">Together</div><div class="detail-row-value">A profile can have a default workspace, but you can still switch workspaces for a session. Profiles answer “who is working?”; workspaces answer “where are they working?”</div></div>
      </div>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _profileMode = 'read';
  _currentProfileDetail = null;
  _setProfileHeaderButtons('empty');
}

function _renderProfileDetail(p, activeName){
  // Tear down any active gateway poller from a previous render before
  // re-wiring this one. _renderProfileDetail is called from three paths
  // (openProfileDetail, loadProfilesPanel periodic refresh, cancelProfileForm
  // restore) — every path must pass through cleanup to avoid leaked pollers.
  _stopAllGatewayPollers();
  _currentProfileDetail = p;
  const title = $('profileDetailTitle');
  const body = $('profileDetailBody');
  const empty = $('profileDetailEmpty');
  if (!title || !body) return;
  title.textContent = p.name;
  const isActive = p.name === activeName;
  const isDefault = !!p.is_default;

  // Profile screen rework v3 (2026-05-14):
  //   Row 1 — hero dossier (256×256 avatar + activity line + description + inline actions)
  //   Row 2 — runtime panel · gateway tile · skills tile (3-col)
  //   Row 3 — files grid (Lucide icons)
  body.innerHTML = `
    <div class="main-view-content profile-detail-v3">
      ${_profileOpsAvatarDialog(p)}
      ${_profileHeroDossier(p, isActive, isDefault)}
      <div class="profile-ops-grid" aria-label="${esc(p.name)} operations controls">
        ${_profileRuntimePanel(p, isActive)}
        ${_profileGatewayTile(p)}
        ${_profileSkillsTile(p, isActive)}
        ${_profileContextCompressionTile(p)}
        ${_profileWorkstepBudgetTile(p)}
        ${_profileToolAccessTile(p)}
      </div>
      ${_profileFilesSection(p)}
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _profileMode = 'read';
  _setProfileHeaderButtons('read', p, activeName);
  const runtimeHydrationSeq = _primeProfileRuntimeControls(p);
  _hydrateProfileDescription(p).catch(e=>console.warn('profile description failed',e));
  _hydrateProfileActivity(p).catch(e=>console.warn('profile activity failed',e));
  _hydrateProfileRuntimeSettings(p, runtimeHydrationSeq).catch(e=>console.warn('profile runtime settings failed',e));
  _loadProfileSkillsTile(p).catch(e=>console.warn('profile skills tile failed',e));
  _bindProfileOpsConsole(p, isActive, isDefault);
}

// ── v3 helpers ───────────────────────────────────────────────────────────
// Placeholder helpers landed here; their full bodies are filled in by
// subsequent commits in the rework series (Tasks 9-13 + 15). Keeping them
// as no-op-renderers so the skeleton still produces a working page during
// the transition.
function _profileHeroDossier(p, isActive, isDefault){
  const name = esc(p.name);
  // Active is conveyed by the inline pill — there is intentionally no bare
  // status dot next to the profile name in v3 (the pill carries it).
  const activeControl = isActive
    ? `<span class="profile-active-pill"><span class="profile-active-pill__dot" aria-hidden="true"></span>Active</span>`
    : `<button id="opsMakeActive" class="profile-active-pill profile-active-pill--action" type="button" aria-label="Make ${name} active profile">Make Active</button>`;
  // The destructive Remove item is gated for the default profile by both
  // a `disabled` attribute on the menu item AND a guard in the click handler.
  const removeDisabled = isDefault ? 'disabled aria-disabled="true"' : '';
  const removeTitle = isDefault
    ? 'The default profile cannot be removed.'
    : 'Permanently delete this profile.';
  const avatarShape = (typeof _normalizeProfileAvatarShape==='function') ? _normalizeProfileAvatarShape(p.avatar_shape) : 'circle';
  return `
    <section class="profile-hero" aria-labelledby="profileHeroName">
      <div class="profile-hero-avatar profile-avatar-shape--${esc(avatarShape)}" id="profileHeroAvatar" tabindex="0" role="button" aria-label="Avatar for ${name}. Activate to change.">
        ${_profileAvatarForUi(p, 'profile-avatar--hero')}
        <button id="profileHeroAvatarEdit" type="button" class="profile-hero-avatar-edit" aria-label="Change avatar" title="Change avatar">✎</button>
      </div>
      <div class="profile-hero-body">
        <div id="profileHeroName" class="profile-hero-name">${name}${activeControl ? ' ' + activeControl : ''}</div>
        <div id="profileHeroActivity" class="profile-hero-activity empty" aria-live="polite"><span class="muted">Loading activity…</span></div>
        <div class="profile-hero-description-row">
          <div id="profileHeroDescription" class="profile-hero-description placeholder" role="button" tabindex="0" title="Click to edit description">Loading description…</div>
          <button id="profileHeroDescriptionEdit" type="button" class="profile-hero-description-edit" aria-label="Edit description" title="Edit description">✎</button>
        </div>
        <div class="profile-hero-actions" aria-label="Primary actions for ${name}">
          <label class="profile-response-mode-control">
            <span>Response Style</span>
            <select id="profileResponseModeSelect" class="profile-ops-select" aria-label="Response style for ${name}">
              <option value="">Soul-driven</option>
              <option value="concise">concise</option>
              <option value="technical">technical</option>
              <option value="teacher">teacher</option>
              <option value="kawaii">kawaii</option>
              <option value="hype">hype</option>
            </select>
          </label>
          <label class="profile-default-workspace-control">
            <span>Default space</span>
            <div class="profile-default-workspace-wrap">
              <button class="composer-workspace-chip profile-default-workspace-chip" id="profileDefaultWorkspaceChip" type="button" title="Choose default space for ${name}">
                <span class="composer-workspace-icon" aria-hidden="true">${li('folder',14)}</span>
                <span class="composer-workspace-label profile-default-workspace-value" id="profileDefaultWorkspaceValue">Loading…</span>
                <span class="composer-workspace-chevron" aria-hidden="true">${li('chevron-down',10)}</span>
              </button>
              <button type="button" id="profileDefaultWorkspaceClear" class="profile-runtime-icon-btn profile-default-workspace-clear" title="Clear default space" aria-label="Clear default space">${li('x',14)}</button>
              <div class="ws-dropdown profile-default-workspace-dropdown" id="profileDefaultWorkspaceDropdown"></div>
            </div>
          </label>
        </div>
      </div>
      <div class="profile-hero-menu-host">
        <button id="profileHeroMenuButton" type="button" class="profile-hero-menu-button"
          aria-haspopup="menu" aria-expanded="false" aria-controls="profileHeroMenu"
          aria-label="More profile actions" title="More actions">⋯</button>
        <div id="profileHeroMenu" class="profile-hero-menu" role="menu" hidden>
          <button class="profile-hero-menu-item" type="button" role="menuitem" data-ops-action="rename">Rename…</button>
          <button class="profile-hero-menu-item" type="button" role="menuitem" data-ops-action="edit-description">Change description…</button>
          <button class="profile-hero-menu-item" type="button" role="menuitem" data-ops-action="duplicate">Duplicate…</button>
          <div class="profile-hero-menu-divider" role="separator"></div>
          <button class="profile-hero-menu-item danger" type="button" role="menuitem" data-ops-action="remove"
            ${removeDisabled} title="${removeTitle}">Remove…</button>
        </div>
      </div>
    </section>`;
}

// ── Description + activity hydration (network) ──────────────────────────
//
// The hero "description" is a short, user-authored summary of the profile's
// purpose, stored at webui.description in config.yaml. It is intentionally
// separate from SOUL.md (the agent's persona / voice for the model itself).
// Clicking the line — or the pencil button — opens an inline textarea editor.

const _PROFILE_DESCRIPTION_MAX = 280;
const _PROFILE_DESCRIPTION_PLACEHOLDER = 'Add a short description so you remember what this profile is for.';
let _profileDescriptionCurrent = '';

function _renderProfileDescriptionView(text){
  const host = $('profileHeroDescription');
  if (!host) return;
  _profileDescriptionCurrent = (typeof text === 'string') ? text : '';
  if (_profileDescriptionCurrent) {
    host.textContent = _profileDescriptionCurrent;
    host.classList.remove('placeholder');
  } else {
    host.textContent = _PROFILE_DESCRIPTION_PLACEHOLDER;
    host.classList.add('placeholder');
  }
}

async function _hydrateProfileDescription(profile){
  const host = $('profileHeroDescription');
  if (!host || !profile || !profile.name) return;
  try {
    const data = await api('/api/profile/persona?name=' + encodeURIComponent(profile.name));
    _renderProfileDescriptionView(data && typeof data.description === 'string' ? data.description : '');
  } catch (e) {
    host.textContent = 'Could not load description.';
    host.classList.add('placeholder');
    _profileDescriptionCurrent = '';
  }
}

function _enterProfileDescriptionEdit(profile){
  const host = $('profileHeroDescription');
  if (!host || !profile || !profile.name) return;
  // Idempotent: if already editing, focus the existing textarea instead of re-rendering.
  if (host.dataset.editing === '1') {
    const ta = host.querySelector('textarea');
    if (ta) ta.focus();
    return;
  }
  host.dataset.editing = '1';
  host.classList.remove('placeholder');
  const current = _profileDescriptionCurrent;
  host.innerHTML = `
    <textarea class="profile-hero-description-edit-textarea" maxlength="${_PROFILE_DESCRIPTION_MAX}" rows="3" aria-label="Profile description">${esc(current)}</textarea>
    <div class="profile-hero-description-edit-actions">
      <span class="profile-hero-description-counter muted" aria-live="polite">${current.length}/${_PROFILE_DESCRIPTION_MAX}</span>
      <button type="button" class="profile-ops-button" data-desc-action="cancel">Cancel</button>
      <button type="button" class="profile-ops-button primary" data-desc-action="save">Save</button>
    </div>`;
  const ta = host.querySelector('textarea');
  const counter = host.querySelector('.profile-hero-description-counter');
  if (ta) {
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    ta.addEventListener('input', () => {
      if (counter) counter.textContent = ta.value.length + '/' + _PROFILE_DESCRIPTION_MAX;
    });
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); _exitProfileDescriptionEdit(profile, /*save*/false); }
      else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); _exitProfileDescriptionEdit(profile, /*save*/true); }
    });
  }
  host.querySelectorAll('[data-desc-action]').forEach(btn => {
    btn.addEventListener('click', (ev) => {
      // Stop propagation so the host's click listener doesn't see the bubble
      // AFTER _exitProfileDescriptionEdit has already cleared dataset.editing —
      // the guard there checks editing === '1' and would re-enter edit mode
      // immediately, breaking both Cancel (appears to do nothing) and Save
      // (leaves the editor open after the POST resolves).
      ev.stopPropagation();
      _exitProfileDescriptionEdit(profile, btn.dataset.descAction === 'save');
    });
  });
  // Textarea clicks must not bubble to the host either — the host guard checks
  // dataset.editing === '1' which is currently true, so the bug only bites
  // when the inner handler flips that flag first, but defense in depth is cheap.
  const taEl = host.querySelector('textarea');
  if (taEl) taEl.addEventListener('click', (ev) => ev.stopPropagation());
}

async function _exitProfileDescriptionEdit(profile, save){
  const host = $('profileHeroDescription');
  if (!host) return;
  if (!save) {
    host.dataset.editing = '';
    _renderProfileDescriptionView(_profileDescriptionCurrent);
    return;
  }
  const ta = host.querySelector('textarea');
  if (!ta) return;
  const next = (ta.value || '').trim();
  if (next === _profileDescriptionCurrent) {
    host.dataset.editing = '';
    _renderProfileDescriptionView(_profileDescriptionCurrent);
    return;
  }
  // Optimistic: show "Saving…" while the POST is in flight.
  host.dataset.editing = '';
  host.textContent = 'Saving…';
  host.classList.add('placeholder');
  try {
    const result = await api('/api/profile/settings', {
      method: 'POST',
      body: JSON.stringify({ name: profile.name, description: next }),
    });
    const saved = (result && typeof result.description === 'string') ? result.description : next;
    _renderProfileDescriptionView(saved);
  } catch (e) {
    console.warn('save description failed', e);
    _renderProfileDescriptionView(_profileDescriptionCurrent);
    if (typeof toast === 'function') toast('Could not save description');
  }
}

function _formatRelativeTime(iso){
  if (!iso) return null;
  const t0 = Date.parse(iso);
  if (!Number.isFinite(t0)) return null;
  const secs = Math.max(1, Math.round((Date.now() - t0) / 1000));
  if (secs < 60) return secs + 's ago';
  const mins = Math.round(secs / 60); if (mins < 60) return mins + ' min ago';
  const hrs = Math.round(mins / 60);  if (hrs < 24) return hrs + ' h ago';
  const days = Math.round(hrs / 24);  if (days === 1) return 'yesterday';
  if (days < 30) return days + ' days ago';
  const months = Math.round(days / 30);
  return months + ' mo ago';
}

async function _hydrateProfileActivity(profile){
  const el = $('profileHeroActivity');
  if (!el || !profile || !profile.name) return;
  try {
    const a = await api('/api/profile/activity?name=' + encodeURIComponent(profile.name));
    const isEmpty = !(a.sessions_week | 0) && !a.last_used_at && !a.gateway_last_run_at;
    if (isEmpty) {
      el.className = 'profile-hero-activity empty';
      el.innerHTML = `<span class="muted">No activity yet — start a chat to see usage here.</span>`;
      return;
    }
    const parts = [];
    if (a.last_used_at) {
      const rel = _formatRelativeTime(a.last_used_at);
      parts.push(`Last used <b>${esc(rel || a.last_used_at)}</b>`);
    }
    if (a.sessions_week) {
      parts.push(`<b>${a.sessions_week}</b> session${a.sessions_week === 1 ? '' : 's'} this week`);
    }
    if (a.spend_week_usd != null) {
      parts.push(`~<b>$${Number(a.spend_week_usd).toFixed(2)}</b> spend`);
    }
    if (a.gateway_last_run_at) {
      const rel = _formatRelativeTime(a.gateway_last_run_at);
      parts.push(`gateway last ran <b>${esc(rel || a.gateway_last_run_at)}</b>`);
    }
    el.className = 'profile-hero-activity';
    el.innerHTML = parts.join(' · ');
  } catch (e) {
    el.className = 'profile-hero-activity empty';
    el.innerHTML = `<span class="muted">Activity unavailable.</span>`;
  }
}
function _profileRuntimePanel(p, isActive){
  // Runtime tile: compact profile-level model routing. All model controls
  // reuse the chat composer's model picker through renderModelDropdown(opts).
  return `
    <article class="profile-ops-tile profile-default-model-tile profile-runtime-tile" aria-labelledby="opsRuntimeTitle">
      <div class="profile-ops-tile-head profile-default-model-head">
        <div class="profile-default-model-titles">
          <span class="profile-ops-tile-label" id="opsRuntimeTitle">Runtime</span>
        </div>
      </div>
      <div class="profile-runtime-rows" aria-label="Runtime controls for ${esc(p.name)}">
        <div class="profile-runtime-group">
          <span class="profile-runtime-row-label">Default Model</span>
          <div class="profile-default-model-chiprow">
            <div class="composer-model-wrap profile-default-model-wrap">
              <button class="composer-model-chip" id="profileDefaultModelChip" type="button" title="Default model for new sessions in this profile">
                <span class="composer-model-icon" aria-hidden="true">${li('cpu',14)}</span>
                <span class="composer-model-label" id="profileDefaultModelLabel">Loading…</span>
                <span class="composer-model-chevron" aria-hidden="true">${li('chevron-down',10)}</span>
              </button>
              <div class="model-dropdown profile-default-model-dropdown" id="profileDefaultModelDropdown"></div>
              <select id="profileDefaultModelSelect" aria-hidden="true" tabindex="-1" style="position:absolute;width:1px;height:1px;clip:rect(0 0 0 0);overflow:hidden;"></select>
            </div>
            <div class="composer-model-wrap profile-default-model-wrap profile-reasoning-wrap">
              <button class="composer-model-chip composer-reasoning-chip" id="profileDefaultReasoningChip" type="button" title="Reasoning effort for new sessions in this profile">
                <span class="composer-model-icon" aria-hidden="true">${li('brain',14)}</span>
                <span class="composer-model-label" id="profileDefaultReasoningLabel">Default</span>
                <span class="composer-model-chevron" aria-hidden="true">${li('chevron-down',10)}</span>
              </button>
              <div class="composer-reasoning-dropdown profile-default-reasoning-dropdown" id="profileDefaultReasoningDropdown"></div>
            </div>
          </div>
        </div>
        <div class="profile-runtime-group">
          <span class="profile-runtime-row-label">Fallback Model</span>
          <div class="profile-default-model-chiprow profile-fallback-chiprow">
            <div class="composer-model-wrap profile-default-model-wrap">
              <button class="composer-model-chip" id="profileFallbackModelChip" type="button" title="Fallback model for this profile">
                <span class="composer-model-icon" aria-hidden="true">${li('corner-down-right',14)}</span>
                <span class="composer-model-label" id="profileFallbackModelLabel">None</span>
                <span class="composer-model-chevron" aria-hidden="true">${li('chevron-down',10)}</span>
              </button>
              <div class="model-dropdown profile-default-model-dropdown" id="profileFallbackModelDropdown"></div>
              <select id="profileFallbackModelSelect" aria-hidden="true" tabindex="-1" style="position:absolute;width:1px;height:1px;clip:rect(0 0 0 0);overflow:hidden;"></select>
            </div>
            <button type="button" id="profileFallbackClear" class="profile-runtime-icon-btn" title="Clear fallback model" aria-label="Clear fallback model">${li('x',14)}</button>
          </div>
        </div>
        <button type="button" id="profileAuxModelsButton" class="profile-ops-button profile-aux-models-button profile-runtime-footer-action">
          ${li('sliders-horizontal',14)}<span>Configure auxiliary tool models</span>
        </button>
      </div>
    </article>`;
}

function _profileContextCompressionTile(p){
  return `
    <article class="profile-ops-tile profile-context-compression-tile" aria-labelledby="profileCompressionTitle">
      <div class="profile-ops-tile-head">
        <span class="profile-ops-tile-label" id="profileCompressionTitle">Context compression</span>
      </div>
      <span class="profile-ops-tile-note">Auto-compress long chats</span>
      <input id="profileCompressionThreshold" class="profile-range" type="range" min="10" max="95" step="5" value="50" aria-label="Compression threshold">
      <div class="profile-compression-detail-row">
        <span id="profileCompressionSummary">Threshold 50%, protect last 20 messages.</span>
        <label class="profile-mini-number">Protect <input id="profileCompressionProtectLast" type="number" min="0" max="200" step="1" value="20"></label>
      </div>
    </article>`;
}

function _profileWorkstepBudgetTile(p){
  return `
    <article class="profile-ops-tile profile-workstep-budget-tile" aria-labelledby="profileWorkstepTitle">
      <div class="profile-ops-tile-head">
        <span class="profile-ops-tile-label" id="profileWorkstepTitle">Work-step budget</span>
      </div>
      <input id="profileMaxTurnsSlider" class="profile-range" type="range" min="10" max="1000" step="10" value="150" aria-label="Work-step budget">
      <input id="profileMaxTurnsInput" class="profile-number-input" type="number" min="1" max="1000" step="1" value="150" aria-label="Work-step budget value">
      <span class="profile-ops-tile-note">agent/tool iterations per user turn</span>
    </article>`;
}

const _PROFILE_TOOLSET_GROUPS = [
  ['terminal', 'Terminal'],
  ['file', 'Files'],
  ['web', 'Web'],
  ['browser', 'Browser'],
  ['cronjob', 'Cron'],
  ['image_gen', 'Image'],
];

function _profileToolAccessTile(p){
  const buttons = _PROFILE_TOOLSET_GROUPS.map(([id,label]) =>
    `<button type="button" class="profile-toolset-pill" data-toolset="${esc(id)}" aria-pressed="false">${esc(label)}</button>`
  ).join('');
  return `
    <article class="profile-ops-tile profile-tool-access-tile" aria-labelledby="profileToolAccessTitle">
      <div class="profile-ops-tile-head">
        <span class="profile-ops-tile-label" id="profileToolAccessTitle">Tool access</span>
      </div>
      <span class="profile-ops-tile-note">Friendly capability groups instead of raw toolset IDs by default.</span>
      <div class="profile-toolset-pills">${buttons}</div>
    </article>`;
}

// ── Gateway tile state (v3 — 5-phase, toggle UX) ──────────────────────
//
// Per-profile state cache, keyed by profile name. The tile DOM holds at
// most one profile's tile at a time (the detail view), but the map
// persists across detail-open/close so a re-open shows the last known
// phase before the first status poll lands.
const _gatewayStateByProfile = new Map();
// Active pollers keyed by profile name → setTimeout handle.
const _gatewayPollers = new Map();
// ── Per-profile messaging-platforms cache (spec 2026-05-16) ───────────
// Map<name → {ok, platforms:[...], message?, fetchedAt}>. Shared between
// the gateway tile's count badge and the modal's first render so we only
// pay for one /api/profile/gateway/platforms fetch per 30s window.
const _platformsByProfile = new Map();
const _PLATFORMS_CACHE_TTL_MS = 30000;
const _GATEWAY_TRANSIENT_PHASES = new Set(['starting', 'stopping']);
const _GATEWAY_INFO_PHASES = new Set(['failed', 'unknown', 'unavailable']);
const _GATEWAY_TRANSIENT_POLL_MS = 1500;
const _GATEWAY_STABLE_POLL_MS = 12000;

function _gatewayLabelForPhase(phase){
  switch(phase){
    case 'starting': return 'Starting';
    case 'running': return 'Running';
    case 'stopping': return 'Stopping';
    case 'failed': return 'Start Failed';
    case 'unknown': return 'Unknown';
    case 'unavailable': return 'Unavailable';
    default: return 'Stopped';
  }
}

function _gatewayToggleLabelForPhase(phase){
  switch(phase){
    case 'starting': return 'Starting…';
    case 'running': return 'On';
    case 'stopping': return 'Stopping…';
    case 'failed': return 'Failed';
    case 'unknown': return 'Check status';
    case 'unavailable': return 'Unavailable';
    default: return 'Off';
  }
}

function _gatewayInfoSummary(state){
  if (!state) return 'Gateway status detail unavailable.';
  const phase = state.phase || 'unknown';
  const reason = state.health && state.health.reason ? state.health.reason : 'no reason provided';
  const detail = state.detail || state.last_error || 'No additional detail.';
  return `${_gatewayLabelForPhase(phase)}: ${reason}. ${detail}`.slice(0, 220);
}

function _gatewayInfoReason(state){
  return state && state.health && state.health.reason ? state.health.reason : '—';
}

function _gatewayInfoVisible(phase){
  return _GATEWAY_INFO_PHASES.has(phase || 'stopped');
}

function _profileCardGatewayPhase(profile){
  const seedPhase = profile && profile.gateway_running ? 'running' : 'stopped';
  if (!profile || !profile.name) return seedPhase;
  if (!_gatewayStateByProfile.has(profile.name)) {
    _gatewayStateByProfile.set(profile.name, {
      phase: seedPhase, last_error: null, phase_started_at: null, pid: null,
    });
  }
  const state = _gatewayStateByProfile.get(profile.name);
  return (state && state.phase) || seedPhase;
}

function _profileCardGatewayLabel(phase){
  const normalized = phase || 'stopped';
  if (normalized === 'running') return t('profile_gateway_running');
  if (normalized === 'stopped') return t('profile_gateway_stopped');
  return `Gateway ${_gatewayLabelForPhase(normalized).toLowerCase()}`;
}

function _repaintProfileCardGateway(profileName, phase){
  if (!profileName) return;
  const label = _profileCardGatewayLabel(phase);
  document.querySelectorAll(`.profile-card-gateway[data-profile-name="${CSS.escape(profileName)}"]`).forEach(icon => {
    icon.setAttribute('data-state', phase || 'stopped');
    icon.setAttribute('title', label);
    const sr = icon.nextElementSibling;
    if (sr && sr.classList && sr.classList.contains('sr-only')) sr.textContent = label;
  });
}

function _repaintGatewayTile(profileName){
  const state = _gatewayStateByProfile.get(profileName);
  if (!state) return;
  const tile = document.querySelector(`.profile-gateway-tile[data-profile-name="${CSS.escape(profileName)}"]`);
  const phase = state.phase || 'stopped';
  _repaintProfileCardGateway(profileName, phase);
  if (!tile) return;  // tile not currently rendered for this profile
  const controlUnavailable = state.control_available === false || phase === 'unavailable';
  const infoVisible = _gatewayInfoVisible(phase);
  const summary = _gatewayInfoSummary(state);
  const wifi = tile.querySelector('#profileGatewayWifi');
  const info = tile.querySelector('[data-gateway-info]');
  const toggle = tile.querySelector('#opsGatewayToggle');
  const toggleLabel = tile.querySelector('#opsGatewayToggleLabel');
  if (wifi) wifi.setAttribute('data-state', phase);
  if (info) {
    info.hidden = !infoVisible;
    info.setAttribute('data-tooltip', summary);
    info.setAttribute('aria-label', `View gateway status details for ${profileName}`);
  }
  if (toggle) {
    toggle.setAttribute('data-state', phase);
    toggle.setAttribute('aria-checked', phase === 'running' ? 'true' : 'false');
    toggle.setAttribute('data-error', infoVisible ? summary : '');
    toggle.disabled = controlUnavailable || _GATEWAY_TRANSIENT_PHASES.has(phase);
    toggle.setAttribute('aria-disabled', toggle.disabled ? 'true' : 'false');
  }
  if (toggleLabel) toggleLabel.textContent = _gatewayToggleLabelForPhase(phase);
  // Refresh the platforms button count from the cache. The button itself
  // is part of .profile-gateway-control; we only touch its count span and
  // its disabled/title state when the cache reports hermes-agent missing.
  const platformsBtn = tile.querySelector('.profile-gateway-platforms-btn');
  if (platformsBtn) {
    const cached = _platformsByProfile.get(profileName);
    const countNode = platformsBtn.querySelector('[data-platforms-count]');
    if (countNode) countNode.textContent = String(_platformsConfiguredCount(cached));
    const unavailable = !!(cached && cached.ok === false);
    platformsBtn.disabled = unavailable;
    platformsBtn.setAttribute('aria-disabled', unavailable ? 'true' : 'false');
    if (unavailable && cached && cached.message) {
      platformsBtn.setAttribute('title', cached.message);
      platformsBtn.setAttribute('aria-label', cached.message);
    }
  }
}

// ── Messaging-platforms config (spec 2026-05-16) ──────────────────────
//
// Per-profile messaging-platform credentials live in <HERMES_HOME>/.env
// and are edited through a modal launched from the Gateway tile. The
// modal is profile-scoped — every call resolves the profile from the
// `name` argument, never from active state — and reuses the existing
// `.profile-skills-manager-overlay` chrome.
//
// The cache (`_platformsByProfile`, declared above) is shared between
// the tile's count badge and the modal's first paint so we only pay
// for one /api/profile/gateway/platforms fetch per 30s window. The
// modal refreshes the cache in the background on open and after every
// Save/Clear.

function _platformsConfiguredCount(payload){
  if (!payload || payload.ok === false || !Array.isArray(payload.platforms)) return 0;
  let n = 0;
  for (const pf of payload.platforms) {
    if (pf && pf.status === 'configured') n++;
  }
  return n;
}

async function _loadProfilePlatforms(name, opts){
  if (!name) return null;
  const force = !!(opts && opts.force);
  const cached = _platformsByProfile.get(name);
  const now = Date.now();
  if (!force && cached && cached.fetchedAt && (now - cached.fetchedAt) < _PLATFORMS_CACHE_TTL_MS) {
    return cached;
  }
  try {
    const fresh = await api('/api/profile/gateway/platforms?name=' + encodeURIComponent(name));
    fresh.fetchedAt = Date.now();
    _platformsByProfile.set(name, fresh);
    return fresh;
  } catch (e) {
    // Surface as ok:false so the UI degrades the same way as the
    // server-side "hermes-agent not available" path.
    const errPayload = { ok: false, message: e && e.message ? e.message : String(e), fetchedAt: Date.now() };
    _platformsByProfile.set(name, errPayload);
    return errPayload;
  }
}

function _renderPlatformCard(platform, profileName){
  if (!platform) return '';
  const key = esc(platform.key || '');
  const label = esc(platform.label || platform.key || 'Unknown');
  const emoji = esc(platform.emoji || '🔌');
  const status = platform.status === 'configured' ? 'configured'
               : platform.status === 'partial' ? 'partial'
               : 'not_configured';
  const statusLabel = status === 'configured' ? '● Configured'
                    : status === 'partial' ? '● Partial'
                    : '○ Not configured';
  // Default expansion: partial expanded (call-to-action), others collapsed.
  // Not-configured cards expand on click; configured cards collapse to
  // show a clean status row and expand to reveal Replace controls.
  const openByDefault = status === 'partial';
  const openClass = openByDefault ? ' open' : '';
  const isPlugin = !!platform.is_plugin;
  let fieldsHTML = '';
  if (isPlugin) {
    const required = Array.isArray(platform.required_env) ? platform.required_env : [];
    const setEnv = new Set(Array.isArray(platform.set_env) ? platform.set_env : []);
    fieldsHTML += `<div class="pf-plugin-note">Plugin platform — schema-light fallback. Set the required environment variables directly.</div>`;
    for (const varName of required) {
      const isSet = setEnv.has(varName);
      // Plugin fields default to password (no metadata available); show
      // Replace pattern if currently set, empty input otherwise.
      fieldsHTML += `
        <div class="platforms-field">
          <div class="pf-field-label"><span>${esc(varName)}</span></div>
          ${isSet ? `
            <div class="pf-secret-row">
              <input class="pf-input" type="password" value="••••••••••" disabled aria-label="${esc(varName)} (set)">
              <button type="button" class="pf-secret-replace" data-pf-replace="${esc(varName)}">Replace</button>
            </div>
          ` : `
            <input class="pf-input" type="password" name="${esc(varName)}" autocomplete="off" spellcheck="false">
          `}
        </div>`;
    }
  } else {
    const vars = Array.isArray(platform.vars) ? platform.vars : [];
    for (const v of vars) {
      const varName = esc(v.name || '');
      const prompt = esc(v.prompt || v.name || '');
      const help = v.help ? `<div class="pf-field-help">${esc(v.help)}</div>` : '';
      const optionalBadge = v.required === false
        ? `<span class="pf-field-optional">optional</span>`
        : '';
      const missingRequired = (v.required !== false) && !v.is_set;
      const labelExtra = missingRequired && status === 'partial'
        ? `<span class="pf-field-required-missing">required · missing</span>` : '';
      if (v.password) {
        if (v.is_set) {
          fieldsHTML += `
            <div class="platforms-field" data-required="${v.required === false ? 'false' : 'true'}">
              <div class="pf-field-label"><span>${prompt}</span>${optionalBadge}${labelExtra}</div>
              <div class="pf-secret-row">
                <input class="pf-input" type="password" value="••••••••••••••••••" disabled aria-label="${prompt} (set)">
                <button type="button" class="pf-secret-replace" data-pf-replace="${varName}">Replace</button>
              </div>
              ${help}
            </div>`;
        } else {
          fieldsHTML += `
            <div class="platforms-field${missingRequired ? ' is-missing' : ''}" data-required="${v.required === false ? 'false' : 'true'}">
              <div class="pf-field-label"><span>${prompt}</span>${optionalBadge}${labelExtra}</div>
              <input class="pf-input" type="password" name="${varName}" autocomplete="off" spellcheck="false" placeholder="">
              ${help}
            </div>`;
        }
      } else {
        // Non-password values round-trip — pre-fill from `value` when present.
        const val = typeof v.value === 'string' ? esc(v.value) : '';
        fieldsHTML += `
          <div class="platforms-field" data-required="${v.required === false ? 'false' : 'true'}">
            <div class="pf-field-label"><span>${prompt}</span>${optionalBadge}${labelExtra}</div>
            <input class="pf-input" type="text" name="${varName}" value="${val}" autocomplete="off" spellcheck="false">
            ${help}
          </div>`;
      }
    }
  }
  return `
    <article class="platforms-card${openClass}" data-platform-key="${key}" data-platform-status="${status}">
      <div class="platforms-card-head" data-platforms-toggle>
        <span class="platforms-card-title">
          <span class="platforms-card-emoji">${emoji}</span>
          <span class="platforms-card-name">${label}${isPlugin ? ' <span class="platforms-card-plugin-tag">plugin</span>' : ''}</span>
        </span>
        <span class="platforms-card-right">
          <span class="pf-status" data-status="${status}">${statusLabel}</span>
          <span class="platforms-card-chevron" aria-hidden="true">›</span>
        </span>
      </div>
      <div class="platforms-card-body">
        ${fieldsHTML}
        <div class="pf-actions">
          <button type="button" class="pf-btn danger" data-platform-clear="${key}">Clear</button>
          <div class="pf-actions-right">
            <button type="button" class="pf-btn" data-platform-cancel="${key}">Cancel</button>
            <button type="button" class="pf-btn primary" data-platform-save="${key}">Save ${label}</button>
          </div>
        </div>
      </div>
    </article>`;
}

function _platformsModalEscape(ev){
  if (ev.key === 'Escape') _closePlatformsManager();
}

function _closePlatformsManager(){
  const overlay = document.getElementById('profilePlatformsManagerOverlay');
  if (overlay) overlay.remove();
  document.removeEventListener('keydown', _platformsModalEscape, true);
}

async function _openPlatformsManager(profileName){
  if (!profileName) return;
  // Render the overlay shell immediately with a loading state so the
  // modal pops without waiting for the fetch.
  _closePlatformsManager();
  const overlay = document.createElement('div');
  overlay.id = 'profilePlatformsManagerOverlay';
  // Reuse the existing skills-manager overlay chrome for backdrop, blur,
  // and centering — keeps modal styling consistent across the WebUI.
  overlay.className = 'profile-skills-manager-overlay platforms-manager-overlay';
  overlay.innerHTML = `
    <div class="profile-skills-manager-card platforms-manager-card" role="dialog" aria-modal="true" aria-labelledby="profilePlatformsManagerTitle">
      <div class="profile-skills-manager-head">
        <div class="profile-skills-manager-head-text">
          <div class="profile-skills-manager-kicker">Messaging</div>
          <h3 class="profile-skills-manager-title" id="profilePlatformsManagerTitle">Messaging platforms for <span id="profilePlatformsManagerName">${esc(profileName)}</span></h3>
          <div class="profile-skills-manager-subtitle">Credentials are saved to this profile's .env file. Toggle the gateway after saving to verify connection.</div>
        </div>
        <div class="profile-skills-manager-actions">
          <button type="button" class="profile-skills-manager-close" aria-label="Close" data-platforms-close>&times;</button>
        </div>
      </div>
      <div id="profilePlatformsManagerBody" class="platforms-manager-list">
        <div class="profile-skills-loading">Loading messaging platforms…</div>
      </div>
      <div class="profile-skills-manager-footer platforms-manager-footer">
        <span class="platforms-manager-hint" id="profilePlatformsManagerHint"></span>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  // Close affordances. Background click closes; inner card click does NOT
  // propagate so Save/Cancel/Replace inside the card cannot race the host
  // listener and silently re-enter edit mode (project memory:
  // feedback_inline_editor_click_bubble).
  overlay.addEventListener('click', (ev) => {
    if (ev.target === overlay) _closePlatformsManager();
  });
  const card = overlay.querySelector('.platforms-manager-card');
  if (card) card.addEventListener('click', (ev) => { ev.stopPropagation(); });
  const closeBtn = overlay.querySelector('[data-platforms-close]');
  if (closeBtn) closeBtn.onclick = (ev) => { ev.stopPropagation(); _closePlatformsManager(); };
  document.addEventListener('keydown', _platformsModalEscape, true);

  // Fetch fresh payload. _loadProfilePlatforms uses the 30s TTL cache,
  // so if the tile already warmed it on render we paint instantly.
  const payload = await _loadProfilePlatforms(profileName, { force: false });
  if (!document.getElementById('profilePlatformsManagerOverlay')) return;
  _paintPlatformsManagerBody(profileName, payload);
}

function _paintPlatformsManagerBody(profileName, payload){
  const body = document.getElementById('profilePlatformsManagerBody');
  if (!body) return;
  if (!payload || payload.ok === false) {
    const msg = (payload && payload.message) || 'hermes-agent not available';
    body.innerHTML = `<div class="profile-skills-error">${esc(msg)}</div>`;
    return;
  }
  const platforms = Array.isArray(payload.platforms) ? payload.platforms : [];
  if (platforms.length === 0) {
    body.innerHTML = `<div class="profile-skills-empty">No platforms available.</div>`;
    return;
  }
  body.innerHTML = platforms.map(pf => _renderPlatformCard(pf, profileName)).join('');
  _bindPlatformsManagerHandlers(profileName, body);
}

function _bindPlatformsManagerHandlers(profileName, body){
  body.querySelectorAll('[data-platforms-toggle]').forEach(head => {
    head.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const card = head.closest('.platforms-card');
      if (card) card.classList.toggle('open');
    });
  });
  body.querySelectorAll('[data-pf-replace]').forEach(btn => {
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const varName = btn.getAttribute('data-pf-replace') || '';
      const field = btn.closest('.platforms-field');
      if (!field) return;
      field.innerHTML = `
        <div class="pf-field-label"><span>${esc(varName)}</span></div>
        <input class="pf-input" type="password" name="${esc(varName)}" autocomplete="off" spellcheck="false" placeholder="" autofocus>`;
      const input = field.querySelector('input');
      if (input) input.focus();
    });
  });
  body.querySelectorAll('[data-platform-cancel]').forEach(btn => {
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const card = btn.closest('.platforms-card');
      if (!card) return;
      // Re-render the card from the cached payload so unsaved edits are
      // discarded. Then collapse it.
      const key = card.getAttribute('data-platform-key');
      const cached = _platformsByProfile.get(profileName);
      const pf = cached && Array.isArray(cached.platforms)
        ? cached.platforms.find(x => x.key === key) : null;
      if (pf) {
        const tmp = document.createElement('div');
        tmp.innerHTML = _renderPlatformCard(pf, profileName);
        const fresh = tmp.firstElementChild;
        if (fresh) {
          card.replaceWith(fresh);
          _bindPlatformsManagerHandlers(profileName, body);
        }
      } else {
        card.classList.remove('open');
      }
    });
  });
  body.querySelectorAll('[data-platform-save]').forEach(btn => {
    btn.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      const key = btn.getAttribute('data-platform-save') || '';
      const card = btn.closest('.platforms-card');
      if (!card) return;
      const values = {};
      card.querySelectorAll('input[name]').forEach(inp => {
        // Skip disabled "set" inputs (the •••• placeholder for password
        // fields that are already set and not currently being replaced).
        if (inp.disabled) return;
        values[inp.name] = inp.value;
      });
      btn.disabled = true;
      btn.textContent = 'Saving…';
      try {
        await _savePlatform(profileName, key, values);
        _setPlatformsManagerHint('Saved. Toggle the gateway to verify connection.');
      } catch (e) {
        _setPlatformsManagerHint('Save failed: ' + (e && e.message ? e.message : String(e)));
      } finally {
        btn.disabled = false;
      }
    });
  });
  body.querySelectorAll('[data-platform-clear]').forEach(btn => {
    btn.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      const key = btn.getAttribute('data-platform-clear') || '';
      btn.disabled = true;
      try {
        await _clearPlatform(profileName, key);
        _setPlatformsManagerHint('Cleared. Toggle the gateway to apply.');
      } catch (e) {
        _setPlatformsManagerHint('Clear failed: ' + (e && e.message ? e.message : String(e)));
      } finally {
        btn.disabled = false;
      }
    });
  });
}

function _setPlatformsManagerHint(text){
  const hint = document.getElementById('profilePlatformsManagerHint');
  if (hint) hint.textContent = text || '';
}

async function _savePlatform(name, platformKey, values){
  if (!name || !platformKey) return null;
  const result = await api('/api/profile/gateway/platform?name=' + encodeURIComponent(name), {
    method: 'POST',
    body: JSON.stringify({ platform: platformKey, values: values || {} }),
  });
  // Refresh cache, repaint tile count, and repaint the modal so the
  // saved card flips to its new status (configured/partial) in place.
  const fresh = await _loadProfilePlatforms(name, { force: true });
  _repaintGatewayTile(name);
  const body = document.getElementById('profilePlatformsManagerBody');
  if (body) _paintPlatformsManagerBody(name, fresh);
  return result;
}

async function _clearPlatform(name, platformKey){
  if (!name || !platformKey) return null;
  const result = await api('/api/profile/gateway/platform?name=' + encodeURIComponent(name) +
                           '&platform=' + encodeURIComponent(platformKey), {
    method: 'DELETE',
  });
  const fresh = await _loadProfilePlatforms(name, { force: true });
  _repaintGatewayTile(name);
  const body = document.getElementById('profilePlatformsManagerBody');
  if (body) _paintPlatformsManagerBody(name, fresh);
  return result;
}

function _profileGatewayTile(p){
  const name = esc(p.name);
  // Provisional phase from the gateway_running flag; the live state
  // map (if present) wins. The poller is the authoritative updater.
  const seedPhase = p.gateway_running ? 'running' : 'stopped';
  if (!_gatewayStateByProfile.has(p.name)) {
    _gatewayStateByProfile.set(p.name, {
      phase: seedPhase, last_error: null, phase_started_at: null, pid: null,
    });
  }
  const state = _gatewayStateByProfile.get(p.name);
  const phase = state.phase || 'stopped';
  const toggleLabel = _gatewayToggleLabelForPhase(phase);
  const summary = _gatewayInfoSummary(state);
  const infoHidden = _gatewayInfoVisible(phase) ? '' : ' hidden';
  const controlUnavailable = state.control_available === false || phase === 'unavailable' || _GATEWAY_TRANSIENT_PHASES.has(phase);
  const isRunning = phase === 'running';
  const ariaChecked = isRunning ? 'true' : 'false';
  // Platforms button: rendered unconditionally in every gateway phase.
  // Credential configuration is independent of lifecycle control — the
  // button is only disabled when hermes-agent is not importable on the
  // server (cached payload has ok=false).
  const platformsCached = _platformsByProfile.get(p.name);
  const platformsCount = _platformsConfiguredCount(platformsCached);
  const platformsBtnDisabled = !!(platformsCached && platformsCached.ok === false);
  const platformsTitle = platformsBtnDisabled
    ? (platformsCached.message || 'hermes-agent not available')
    : `Configure messaging platforms for ${p.name}`;
  return `
    <article class="profile-ops-tile profile-gateway-tile" aria-labelledby="opsGatewayTitle" data-profile-name="${name}">
      <div class="profile-ops-tile-head">
        <span class="profile-ops-tile-label profile-gateway-label" id="opsGatewayTitle">Agent Gateway</span>
        <span class="profile-wifi profile-wifi-xl" id="profileGatewayWifi" data-state="${phase}" aria-hidden="true">${li('wifi',36)}</span>
      </div>
      <div class="profile-gateway-control">
        <div class="profile-gateway-toggle-group">
          <button id="opsGatewayToggle" class="profile-gateway-toggle" type="button"
                  role="switch" aria-checked="${ariaChecked}" aria-disabled="${controlUnavailable ? 'true' : 'false'}"
                  ${controlUnavailable ? 'disabled' : ''}
                  data-profile-name="${name}" data-gateway-toggle data-state="${phase}"
                  data-error="${esc(summary)}">
            <span class="profile-gateway-toggle-track" aria-hidden="true">
              <span class="profile-gateway-toggle-thumb"></span>
            </span>
            <span class="profile-gateway-toggle-label" id="opsGatewayToggleLabel">${esc(toggleLabel)}</span>
          </button>
          <button type="button" class="profile-gateway-info has-tooltip has-tooltip--bottom-right has-tooltip--wrap"
                  data-gateway-info data-tooltip="${esc(summary)}"
                  aria-label="View gateway status details for ${name}"${infoHidden}>ⓘ</button>
        </div>
        <button type="button" class="profile-gateway-platforms-btn"
                data-profile-name="${name}" data-platforms-action="${name}"
                title="${esc(platformsTitle)}"
                aria-label="${esc(platformsTitle)}"${platformsBtnDisabled ? ' disabled aria-disabled="true"' : ''}>
          <span class="profile-gateway-platforms-icon" aria-hidden="true">⚙</span>
          <span class="profile-gateway-platforms-label">Platforms · <span class="profile-gateway-platforms-count" data-platforms-count>${platformsCount}</span></span>
        </button>
      </div>
    </article>`;
}

function _profileSkillsTile(p, isActive){
  // Counts must come from /api/profile/skills, which knows the full visible
  // skill universe and this profile's disabled set. /api/profiles skill_count
  // is row metadata and can vary per profile before hydration.
  return `
    <article class="profile-ops-tile" aria-labelledby="opsSkillsTitle">
      <div class="profile-ops-tile-head">
        <span class="profile-ops-tile-label" id="opsSkillsTitle">Skills</span>
        <span class="profile-ops-status-pill" id="opsSkillsPill">
          <span id="opsSkillsDot" class="profile-status-dot off" aria-hidden="true"></span>
          <span id="opsSkillsPillLabel">Loading skills…</span>
        </span>
      </div>
      <div>
        <span class="profile-ops-tile-value" id="opsSkillsValue">Loading skills…</span>
        <div id="opsSkillsTopChips" class="profile-skill-top" role="list"></div>
      </div>
      <div class="profile-ops-control-row">
        <button class="profile-ops-button" type="button" data-ops-action="skills">Manage</button>
      </div>
    </article>`;
}
// _hydrateProfileDescription / _hydrateProfileActivity — bodies above.

// ── Default-model tile (rework 2026-05-15) ─────────────────────────────
//
// The default-model tile shares the chat composer's picker — same chrome
// (.composer-model-chip + .model-dropdown), same renderer
// (renderModelDropdown from ui.js, called with opts pointing at the tile's
// own select + dropdown), same row layout, search, badges, custom-ID
// input. Selecting a row auto-saves to /api/profile/settings. Reasoning
// chip mirrors the chat composer's reasoning options exactly (none ·
// minimal · low · medium · high · xhigh); the chip label reads "Default"
// when no override is set (matches the composer's empty-state label).

let _profileRuntimeSettings = null;
let _profileRuntimeHydrationSeq = 0;
let _profileRuntimeDirty = null;
const _PROFILE_COMPRESSION_DEFAULTS = {
  enabled: true,
  threshold: 0.5,
  target_ratio: 0.5,
  protect_last_n: 20,
  protect_first_n: 0,
};

function _freshProfileRuntimeDirty(){
  return {
    default_model: false,
    default_reasoning: false,
    fallback_model: false,
    response_mode: false,
    compression: false,
    max_turns: false,
    default_workspace: false,
    toolsets: false,
    auxiliary_models: false,
  };
}

function _markProfileRuntimeDirty(key){
  if (!_profileRuntimeDirty) _profileRuntimeDirty = _freshProfileRuntimeDirty();
  if (Object.prototype.hasOwnProperty.call(_profileRuntimeDirty, key)) {
    _profileRuntimeDirty[key] = true;
  }
}

function _isCurrentProfileRuntimeHydration(profileName, seq){
  return Number.isFinite(seq)
    && seq === _profileRuntimeHydrationSeq
    && _currentProfileDetail
    && _currentProfileDetail.name === profileName;
}

function _primeProfileRuntimeControls(profile){
  const seq = ++_profileRuntimeHydrationSeq;
  _profileRuntimeDirty = _freshProfileRuntimeDirty();
  if (!profile || !profile.name) return seq;

  _profileRuntimeSettings = {
    fallback_model: {},
    response_mode: '',
    compression: Object.assign({}, _PROFILE_COMPRESSION_DEFAULTS),
    max_turns: null,
    default_workspace: profile.default_workspace || '',
    toolsets: [],
    toolsets_configured: false,
    auxiliary_models: [],
  };
  _populateProfileDefaultModelSelect(profile);
  _populateProfileModelSelect('profileFallbackModelSelect', '', null);
  _applyProfileDefaultModelChip(profile.model || '');
  _applyProfileDefaultReasoningChip('');
  _applyProfileFallbackModelChip('');
  _applyProfileResponseMode('');
  _applyProfileCompression(_PROFILE_COMPRESSION_DEFAULTS);
  _applyProfileMaxTurns(undefined);
  _applyProfileDefaultWorkspace(profile.default_workspace || '');
  _applyProfileToolsets([], false);
  _wireProfileDefaultModelHandlers(profile.name);
  _wireProfileRuntimeSettingHandlers(profile.name);
  return seq;
}

async function _hydrateProfileRuntimeSettings(profile, seq){
  if (!profile || !profile.name) return;
  const token = Number.isFinite(seq) ? seq : _profileRuntimeHydrationSeq;
  if (!_isCurrentProfileRuntimeHydration(profile.name, token)) return;
  // Wait for the composer's model catalog before mirroring it into the
  // profile's hidden select. Without this, the picker can paint empty
  // immediately after a profile switch.
  const ready = window._modelDropdownReady;
  if (ready && typeof ready.then === 'function') {
    try { await ready; } catch (_) {}
  }
  if (!_isCurrentProfileRuntimeHydration(profile.name, token)) return;
  if (!_profileModelGroupsFromComposer().length && typeof populateModelDropdown === 'function') {
    try { await populateModelDropdown(); } catch (_) {}
  }
  if (!_isCurrentProfileRuntimeHydration(profile.name, token)) return;

  let settings = {};
  try {
    settings = await api('/api/profile/settings?name=' + encodeURIComponent(profile.name) + '&include_avatar=0');
  } catch (_) { settings = {}; }
  if (!_isCurrentProfileRuntimeHydration(profile.name, token)) return;

  const dirty = _profileRuntimeDirty || _freshProfileRuntimeDirty();
  const prior = _profileRuntimeSettings || {};
  const next = Object.assign({}, prior, settings || {});
  if (dirty.default_reasoning) next.reasoning_effort = prior.reasoning_effort || '';
  if (dirty.fallback_model) next.fallback_model = prior.fallback_model || {};
  if (dirty.response_mode) next.response_mode = prior.response_mode || '';
  if (dirty.compression) next.compression = prior.compression || _profileCompressionPayload();
  if (dirty.max_turns) next.max_turns = prior.max_turns;
  if (dirty.default_workspace) next.default_workspace = prior.default_workspace || profile.default_workspace || '';
  if (dirty.toolsets) {
    next.toolsets = prior.toolsets || [];
    next.toolsets_configured = !!prior.toolsets_configured;
  }
  if (dirty.auxiliary_models) next.auxiliary_models = prior.auxiliary_models || [];
  _profileRuntimeSettings = next;

  const fallback = (_profileRuntimeSettings && _profileRuntimeSettings.fallback_model) || {};
  const defaultModelChip = $('profileDefaultModelChip');
  const fallbackModelChip = $('profileFallbackModelChip');
  const currentDefaultModel = defaultModelChip ? (defaultModelChip.dataset.modelValue || '') : '';
  const currentFallbackModel = fallbackModelChip ? (fallbackModelChip.dataset.modelValue || '') : '';
  _populateProfileModelSelect(
    'profileDefaultModelSelect',
    dirty.default_model ? currentDefaultModel : (profile.model || ''),
    profile.provider || null
  );
  _populateProfileModelSelect(
    'profileFallbackModelSelect',
    dirty.fallback_model ? currentFallbackModel : (fallback.model || ''),
    fallback.provider || null
  );
  if (!dirty.default_model) _applyProfileDefaultModelChip(profile.model || '');
  if (!dirty.default_reasoning) {
    _applyProfileDefaultReasoningChip(typeof _profileRuntimeSettings.reasoning_effort === 'string' ? _profileRuntimeSettings.reasoning_effort : '');
  }
  if (!dirty.fallback_model) _applyProfileFallbackModelChip(fallback.model || '');
  if (!dirty.response_mode) _applyProfileResponseMode(_profileRuntimeSettings.response_mode || '');
  if (!dirty.compression) _applyProfileCompression(_profileRuntimeSettings.compression || {});
  if (!dirty.max_turns) _applyProfileMaxTurns(_profileRuntimeSettings.max_turns);
  if (!dirty.default_workspace) _applyProfileDefaultWorkspace(_profileRuntimeSettings.default_workspace || profile.default_workspace || '');
  if (!dirty.toolsets) _applyProfileToolsets(_profileRuntimeSettings.toolsets || [], !!_profileRuntimeSettings.toolsets_configured);
  _wireProfileDefaultModelHandlers(profile.name);
  _wireProfileRuntimeSettingHandlers(profile.name);
}

async function _hydrateProfileDefaultModel(profile){ return _hydrateProfileRuntimeSettings(profile); }

function _populateProfileDefaultModelSelect(profile){
  _populateProfileModelSelect('profileDefaultModelSelect', profile && profile.model || '', profile && profile.provider || null);
}

function _populateProfileModelSelect(selectId, currentModel, preferredProvider){
  // Mirror #modelSelect's optgroups into the profile's hidden select so the
  // composer's renderer (renderModelDropdown) sees the same catalog when
  // called with opts pointing at this select.
  const sel = $(selectId);
  const composerSel = $('modelSelect');
  if (!sel || !composerSel) return;
  sel.innerHTML = '';
  for (const child of Array.from(composerSel.children)) {
    if (child.tagName === 'OPTGROUP') {
      const og = document.createElement('optgroup');
      og.label = child.label || '';
      if (child.dataset && child.dataset.provider) og.dataset.provider = child.dataset.provider;
      for (const opt of Array.from(child.children)) {
        const o = document.createElement('option');
        o.value = opt.value;
        o.textContent = opt.textContent;
        og.appendChild(o);
      }
      sel.appendChild(og);
    } else if (child.tagName === 'OPTION') {
      const o = document.createElement('option');
      o.value = child.value;
      o.textContent = child.textContent;
      sel.appendChild(o);
    }
  }
  // Seed the value to the profile's current model so renderModelDropdown's
  // .active row highlight lands on the right row.
  const current = currentModel || '';
  if (current && typeof _applyModelToDropdown === 'function') {
    _applyModelToDropdown(current, sel, preferredProvider || null);
  } else if (current) {
    sel.value = current;
  }
}

function _applyProfileDefaultModelChip(modelValue){
  const label = $('profileDefaultModelLabel');
  const chip = $('profileDefaultModelChip');
  const text = modelValue
    ? (typeof getModelLabel === 'function' ? getModelLabel(modelValue) : modelValue)
    : 'Select model…';
  if (label) label.textContent = text;
  if (chip) {
    chip.dataset.modelValue = modelValue || '';
    chip.title = 'Default model: ' + text;
  }
}

function _applyProfileFallbackModelChip(modelValue){
  const label = $('profileFallbackModelLabel');
  const chip = $('profileFallbackModelChip');
  const text = modelValue
    ? (typeof getModelLabel === 'function' ? getModelLabel(modelValue) : modelValue)
    : 'None';
  if (label) label.textContent = text;
  if (chip) {
    chip.dataset.modelValue = modelValue || '';
    chip.classList.toggle('inactive', !modelValue);
    chip.title = modelValue ? ('Fallback model: ' + text) : 'No profile-specific fallback model';
  }
  const clear = $('profileFallbackClear');
  if (clear) clear.disabled = !modelValue;
}

function _applyProfileDefaultReasoningChip(effort){
  const norm = String(effort || '').trim().toLowerCase();
  const label = $('profileDefaultReasoningLabel');
  const chip = $('profileDefaultReasoningChip');
  if (!label || !chip) return;
  // Composer parity: chip shows "Default" when no override is set, else the
  // effort label (lowercase, matching the composer chip's display style via
  // _formatReasoningEffortLabel() in ui.js).
  const display = (typeof _formatReasoningEffortLabel === 'function')
    ? _formatReasoningEffortLabel(norm)
    : (norm || 'Default');
  label.textContent = display;
  chip.dataset.effort = norm;
  const inactive = !norm || norm === 'none';
  chip.classList.toggle('inactive', inactive);
}

function _applyProfileResponseMode(mode){
  const sel = $('profileResponseModeSelect');
  if (!sel) return;
  const value = String(mode || '').trim().toLowerCase();
  sel.value = ['concise','technical','teacher','kawaii','hype'].includes(value) ? value : '';
}

function _profileCompressionState(){
  const current = (_profileRuntimeSettings && _profileRuntimeSettings.compression) || {};
  return Object.assign({}, _PROFILE_COMPRESSION_DEFAULTS, current || {});
}

function _applyProfileCompression(raw){
  const c = Object.assign({}, _PROFILE_COMPRESSION_DEFAULTS, raw || {});
  const threshold = $('profileCompressionThreshold');
  const protect = $('profileCompressionProtectLast');
  if (threshold) threshold.value = String(Math.round((Number(c.threshold) || 0.5) * 100));
  if (protect) protect.value = String(Number.isFinite(Number(c.protect_last_n)) ? Number(c.protect_last_n) : 20);
  _syncProfileCompressionSummary();
}

function _syncProfileCompressionSummary(){
  const threshold = $('profileCompressionThreshold');
  const protect = $('profileCompressionProtectLast');
  const summary = $('profileCompressionSummary');
  if (!summary) return;
  const pct = Math.max(10, Math.min(95, parseInt(threshold && threshold.value || '50', 10) || 50));
  const last = Math.max(0, Math.min(200, parseInt(protect && protect.value || '20', 10) || 0));
  summary.textContent = `Threshold ${pct}%, protect last ${last} messages.`;
}

function _applyProfileMaxTurns(value){
  const n = Math.max(1, Math.min(1000, parseInt(value || '150', 10) || 150));
  const slider = $('profileMaxTurnsSlider');
  const input = $('profileMaxTurnsInput');
  if (slider) slider.value = String(Math.max(10, Math.min(1000, Math.round(n / 10) * 10)));
  if (input) input.value = String(n);
}

function _applyProfileDefaultWorkspace(value){
  const path = String(value || '').trim();
  const el = $('profileDefaultWorkspaceValue');
  const chip = $('profileDefaultWorkspaceChip');
  const clear = $('profileDefaultWorkspaceClear');
  const label = path ? getWorkspaceFriendlyName(path) : 'No default space set';
  if (el) el.textContent = label;
  if (chip) {
    chip.dataset.workspace = path;
    chip.classList.toggle('inactive', !path);
    chip.title = path || 'No default space set';
  }
  if (clear) {
    clear.disabled = !path;
    clear.hidden = !path;
  }
}

function _refreshProfileCardRuntimeMeta(profileName){
  const p = (_profilesCache && Array.isArray(_profilesCache.profiles))
    ? _profilesCache.profiles.find(x => x.name === profileName)
    : null;
  if (!p) return;
  const card = document.querySelector(`.profile-card[data-name="${CSS.escape(profileName)}"]`);
  const metaEl = card ? card.querySelector('.profile-card-meta') : null;
  if (!metaEl) return;
  const meta = [];
  if (p.model) meta.push(p.model.split('/').pop());
  if (p.provider) meta.push(p.provider);
  metaEl.textContent = meta.length ? meta.join(' · ') : t('profile_no_configuration');
}

function _applyProfileToolsets(toolsets, configured){
  const defaultToolsets = _PROFILE_TOOLSET_GROUPS.map(([id]) => id);
  const selected = new Set(Array.isArray(toolsets) && (configured || toolsets.length) ? toolsets : defaultToolsets);
  document.querySelectorAll('.profile-toolset-pill[data-toolset]').forEach(btn => {
    const on = selected.has(btn.dataset.toolset);
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

function _wireProfileDefaultModelHandlers(profileName){
  const modelChip = $('profileDefaultModelChip');
  const fallbackChip = $('profileFallbackModelChip');
  const reasoningChip = $('profileDefaultReasoningChip');
  if (modelChip) {
    modelChip.onclick = (ev) => { ev.stopPropagation(); _toggleProfileDefaultModelDropdown(profileName); };
  }
  if (fallbackChip) {
    fallbackChip.onclick = (ev) => { ev.stopPropagation(); _toggleProfileFallbackModelDropdown(profileName); };
  }
  if (reasoningChip) {
    reasoningChip.onclick = (ev) => { ev.stopPropagation(); _toggleProfileDefaultReasoningDropdown(profileName); };
  }
}

function _closeProfileDefaultDropdowns(){
  ['profileDefaultModelDropdown', 'profileFallbackModelDropdown', 'profileDefaultReasoningDropdown'].forEach(id => {
    const el = $(id); if (el) el.classList.remove('open');
  });
  ['profileDefaultModelChip', 'profileFallbackModelChip', 'profileDefaultReasoningChip'].forEach(id => {
    const el = $(id); if (el) el.classList.remove('active');
  });
}

function _toggleProfileDefaultModelDropdown(profileName){
  const dd = $('profileDefaultModelDropdown');
  const chip = $('profileDefaultModelChip');
  const sel = $('profileDefaultModelSelect');
  if (!dd || !chip || !sel) return;
  if (dd.classList.contains('open')) { _closeProfileDefaultDropdowns(); return; }
  // Close any other dropdowns in the page first so two pickers can't be
  // open simultaneously.
  closeWsDropdown();
  if (typeof closeModelDropdown === 'function') closeModelDropdown();
  if (typeof closeReasoningDropdown === 'function') closeReasoningDropdown();
  _closeProfileDefaultDropdowns();
  // Use the composer's parameterised picker — same search input, configured
  // badges, group headings with counts, custom-ID row. (The hidden select
  // is the canonical source of truth for the renderer.)
  renderModelDropdown({
    select: sel,
    dropdown: dd,
    onSelect: (value) => _onProfileDefaultModelPicked(profileName, value),
    onClose: () => _closeProfileDefaultDropdowns(),
    // The tile subtitle already states this scope; suppress the in-dropdown
    // duplicate scope-note for the profile case (composer keeps its default).
    scopeNote: "",
  });
  dd.classList.add('open');
  chip.classList.add('active');
  _positionProfileDefaultDropdown(chip, dd);
}

function _toggleProfileFallbackModelDropdown(profileName){
  const dd = $('profileFallbackModelDropdown');
  const chip = $('profileFallbackModelChip');
  const sel = $('profileFallbackModelSelect');
  if (!dd || !chip || !sel) return;
  if (dd.classList.contains('open')) { _closeProfileDefaultDropdowns(); return; }
  closeWsDropdown();
  if (typeof closeModelDropdown === 'function') closeModelDropdown();
  if (typeof closeReasoningDropdown === 'function') closeReasoningDropdown();
  _closeProfileDefaultDropdowns();
  renderModelDropdown({
    select: sel,
    dropdown: dd,
    onSelect: (value) => _onProfileFallbackModelPicked(profileName, value),
    onClose: () => _closeProfileDefaultDropdowns(),
    scopeNote: "",
  });
  dd.classList.add('open');
  chip.classList.add('active');
  _positionProfileDefaultDropdown(chip, dd);
}

// Flip-up logic: a profile-tile chip can sit near the viewport bottom on
// short laptop screens, where opening downward (.profile-default-model-dropdown
// CSS default: top:calc(100% + 4px)) would clip the option list. If there's
// more headroom above the chip than below it, flip the dropdown above the
// chip instead. The CSS owns the resting position; this helper toggles a
// .flipped class that switches top↔bottom.
function _positionProfileDefaultDropdown(chip, dd){
  if(!chip || !dd) return;
  const chipRect = chip.getBoundingClientRect();
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  const spaceBelow = vh - chipRect.bottom;
  const spaceAbove = chipRect.top;
  const ddHeight = dd.offsetHeight || 320; // matches .model-dropdown max-height
  // Open upward when below-space is too tight AND above-space is more
  // generous. Threshold: dropdown's measured/expected height + a small
  // breathing margin so the last row isn't flush against the viewport edge.
  const minBelow = ddHeight + 12;
  const flip = (spaceBelow < minBelow) && (spaceAbove > spaceBelow);
  dd.classList.toggle('flipped', flip);
}

async function _onProfileDefaultModelPicked(profileName, value){
  _markProfileRuntimeDirty('default_model');
  const sel = $('profileDefaultModelSelect');
  const modelChip = $('profileDefaultModelChip');
  const reasoningChip = $('profileDefaultReasoningChip');
  if (!sel) { _closeProfileDefaultDropdowns(); return; }
  // Capture prior values BEFORE optimistic UI update so a failed POST can
  // revert without re-fetching from the server.
  const priors = {
    selValue: sel.value || '',
    modelValue: modelChip ? (modelChip.dataset.modelValue || '') : '',
    reasoning: reasoningChip ? (reasoningChip.dataset.effort || '') : '',
  };
  // Mirror selectModelFromDropdown's custom-model handling: if the value
  // isn't an existing option (custom ID typed into the picker's input),
  // append a temporary option so sel.value sticks.
  if (!Array.from(sel.options).some(o => o.value === value)) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = getModelLabel(value);
    opt.dataset.custom = '1';
    sel.querySelectorAll('option[data-custom]').forEach(o => o.remove());
    sel.appendChild(opt);
  }
  sel.value = value;
  _applyProfileDefaultModelChip(value);
  _closeProfileDefaultDropdowns();
  await _persistProfileDefaultModel(profileName, priors);
}

async function _onProfileFallbackModelPicked(profileName, value){
  _markProfileRuntimeDirty('fallback_model');
  const sel = $('profileFallbackModelSelect');
  const chip = $('profileFallbackModelChip');
  if (!sel) { _closeProfileDefaultDropdowns(); return; }
  const priors = {
    selValue: sel.value || '',
    modelValue: chip ? (chip.dataset.modelValue || '') : '',
  };
  if (!Array.from(sel.options).some(o => o.value === value)) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = getModelLabel(value);
    opt.dataset.custom = '1';
    sel.querySelectorAll('option[data-custom]').forEach(o => o.remove());
    sel.appendChild(opt);
  }
  sel.value = value;
  _applyProfileFallbackModelChip(value);
  _closeProfileDefaultDropdowns();
  await _persistProfileFallbackModel(profileName, priors);
}

function _toggleProfileDefaultReasoningDropdown(profileName){
  const dd = $('profileDefaultReasoningDropdown');
  const chip = $('profileDefaultReasoningChip');
  if (!dd || !chip) return;
  if (dd.classList.contains('open')) { _closeProfileDefaultDropdowns(); return; }
  closeWsDropdown();
  if (typeof closeModelDropdown === 'function') closeModelDropdown();
  if (typeof closeReasoningDropdown === 'function') closeReasoningDropdown();
  _closeProfileDefaultDropdowns();
  // Use the chat composer's reasoning renderer (parameterised in ui.js) so
  // both surfaces share the same row set + click semantics. The onSelect
  // callback owns the auto-save flow; renderReasoningDropdown attaches
  // ev.stopPropagation() on each row so the composer's document-level
  // .reasoning-option click handler does NOT also fire here.
  renderReasoningDropdown({
    dropdown: dd,
    current: chip.dataset.effort || '',
    onSelect: (value) => _onProfileDefaultReasoningPicked(profileName, value),
  });
  dd.classList.add('open');
  chip.classList.add('active');
  _positionProfileDefaultDropdown(chip, dd);
}

function _onProfileDefaultReasoningPicked(profileName, value){
  _markProfileRuntimeDirty('default_reasoning');
  // Capture prior values BEFORE optimistic UI update so a failed POST can
  // revert without re-fetching from the server.
  const modelChip = $('profileDefaultModelChip');
  const reasoningChip = $('profileDefaultReasoningChip');
  const sel = $('profileDefaultModelSelect');
  const priors = {
    selValue: sel ? (sel.value || '') : '',
    modelValue: modelChip ? (modelChip.dataset.modelValue || '') : '',
    reasoning: reasoningChip ? (reasoningChip.dataset.effort || '') : '',
  };
  _applyProfileDefaultReasoningChip(value);
  _closeProfileDefaultDropdowns();
  _persistProfileDefaultModel(profileName, priors);
}

async function _persistProfileDefaultModel(profileName, priors){
  const token = _profileRuntimeHydrationSeq;
  const modelChip = $('profileDefaultModelChip');
  const reasoningChip = $('profileDefaultReasoningChip');
  const sel = $('profileDefaultModelSelect');
  const modelValue = modelChip ? (modelChip.dataset.modelValue || '') : '';
  // Prior values are captured at the call site (before the optimistic UI
  // update). If a caller doesn't pass them — e.g. legacy callers — fall back
  // to the profiles cache for model/provider; reasoning has no other source.
  const priorProfile = (_profilesCache && Array.isArray(_profilesCache.profiles))
    ? _profilesCache.profiles.find(x => x.name === profileName) : null;
  const priorModel = (priors && typeof priors.modelValue === 'string')
    ? priors.modelValue
    : (priorProfile ? (priorProfile.model || '') : '');
  const priorProvider = priorProfile ? (priorProfile.provider || null) : null;
  const priorReasoning = (priors && typeof priors.reasoning === 'string')
    ? priors.reasoning : '';
  const priorSelValue = (priors && typeof priors.selValue === 'string')
    ? priors.selValue : (sel ? sel.value : '');
  // Derive the provider from the chosen model (its option's optgroup) the
  // same way the chat composer does — _modelStateForSelect handles both
  // explicit @provider:model values and optgroup-tagged options.
  const modelState = (sel && typeof _modelStateForSelect === 'function')
    ? _modelStateForSelect(sel, modelValue)
    : { model: modelValue, model_provider: null };
  const body = {
    name: profileName,
    model: modelState.model || modelValue,
    provider: modelState.model_provider || null,
    reasoning_effort: reasoningChip ? (reasoningChip.dataset.effort || '') : '',
  };
  try {
    const updated = await api('/api/profile/settings', { method: 'POST', body: JSON.stringify(body) });
    // Update the profiles cache so the profile card reflects the new
    // model/provider without an extra fetch.
    const p = (_profilesCache && Array.isArray(_profilesCache.profiles))
      ? _profilesCache.profiles.find(x => x.name === profileName) : null;
    if (p) Object.assign(p, updated);
    if (_isCurrentProfileRuntimeHydration(profileName, token)) {
      Object.assign(_currentProfileDetail, updated);
    }
    // If we're editing the active profile and no chat session has been
    // started yet, mirror onto the chat composer's #modelSelect so the
    // composer chip updates without a reload — matches the legacy
    // _saveProfileModelSettings behaviour.
    const isActive = (S.activeProfile || (_profilesCache && _profilesCache.active) || 'default') === profileName;
    if (isActive) {
      const composerSel = $('modelSelect');
      if (composerSel && updated.model && typeof _applyModelToDropdown === 'function') {
        const resolved = _applyModelToDropdown(updated.model, composerSel, updated.provider || null) || updated.model;
        if (S.session && !(S.messages && S.messages.length)) {
          S.session.model = resolved;
          S.session.model_provider = updated.provider || modelState.model_provider || null;
        }
        if (typeof syncModelChip === 'function') syncModelChip();
        if (typeof syncTopbar === 'function') syncTopbar();
      }
    }
    _refreshProfileCardRuntimeMeta(profileName);
  } catch (e) {
    // Revert the optimistic chip + select-mirror state so the UI no longer
    // claims a value the server doesn't have. Toast the failure (no inline
    // "Saved" diode by design — failure is the only state worth surfacing).
    if (_isCurrentProfileRuntimeHydration(profileName, token)) {
      try {
        if (sel) {
          if (priorSelValue && Array.from(sel.options).some(o => o.value === priorSelValue)) {
            sel.value = priorSelValue;
          } else if (priorModel && typeof _applyModelToDropdown === 'function') {
            _applyModelToDropdown(priorModel, sel, priorProvider);
          }
        }
        _applyProfileDefaultModelChip(priorModel);
        _applyProfileDefaultReasoningChip(priorReasoning);
      } catch (revertErr) {
        console.warn('Default model revert failed:', revertErr);
      }
      showToast('Default model save failed: ' + (e && e.message ? e.message : e));
    }
    console.warn('Default model save failed:', e);
  }
}

async function _persistProfileFallbackModel(profileName, priors){
  const token = _profileRuntimeHydrationSeq;
  const chip = $('profileFallbackModelChip');
  const sel = $('profileFallbackModelSelect');
  const modelValue = chip ? (chip.dataset.modelValue || '') : '';
  const priorModel = (priors && typeof priors.modelValue === 'string') ? priors.modelValue : '';
  const priorSelValue = (priors && typeof priors.selValue === 'string') ? priors.selValue : '';
  const modelState = (sel && modelValue && typeof _modelStateForSelect === 'function')
    ? _modelStateForSelect(sel, modelValue)
    : { model: modelValue, model_provider: null };
  const provider = _profileProviderForPickerValue(sel, modelValue, modelState);
  if (modelValue && !provider) {
    if (sel) {
      if (priorSelValue && Array.from(sel.options).some(o => o.value === priorSelValue)) sel.value = priorSelValue;
      else sel.value = '';
    }
    _applyProfileFallbackModelChip(priorModel);
    showToast('Pick a fallback model from a configured provider.', 4000, 'error');
    return;
  }
  const fallback_model = modelValue ? {
    provider,
    model: modelState.model || modelValue,
  } : {};
  try {
    const updated = await api('/api/profile/settings', {
      method: 'POST',
      body: JSON.stringify({ name: profileName, fallback_model }),
    });
    if (_isCurrentProfileRuntimeHydration(profileName, token)) {
      _profileRuntimeSettings = Object.assign({}, _profileRuntimeSettings || {}, updated || {});
      Object.assign(_currentProfileDetail, updated);
    }
  } catch (e) {
    if (_isCurrentProfileRuntimeHydration(profileName, token)) {
      if (sel) {
        if (priorSelValue && Array.from(sel.options).some(o => o.value === priorSelValue)) sel.value = priorSelValue;
        else sel.value = '';
      }
      _applyProfileFallbackModelChip(priorModel);
      showToast('Fallback model save failed: ' + (e && e.message ? e.message : e), 4000, 'error');
    }
    console.warn('Fallback model save failed:', e);
  }
}

async function _persistProfileSetting(profileName, patch, onFailure){
  const token = _profileRuntimeHydrationSeq;
  try {
    const updated = await api('/api/profile/settings', {
      method: 'POST',
      body: JSON.stringify(Object.assign({ name: profileName }, patch || {})),
    });
    if (_isCurrentProfileRuntimeHydration(profileName, token)) {
      _profileRuntimeSettings = Object.assign({}, _profileRuntimeSettings || {}, updated || {});
      Object.assign(_currentProfileDetail, updated);
    }
    return updated;
  } catch (e) {
    if (_isCurrentProfileRuntimeHydration(profileName, token) && typeof onFailure === 'function') onFailure(e);
    console.warn('Profile setting save failed:', e);
    if (_isCurrentProfileRuntimeHydration(profileName, token)) {
      showToast('Profile setting save failed: ' + (e && e.message ? e.message : e), 4000, 'error');
    }
    throw e;
  }
}

function _positionProfileDefaultWorkspaceDropdown(chip, dd){
  if(!chip || !dd) return;
  const chipRect = chip.getBoundingClientRect();
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  const spaceBelow = vh - chipRect.bottom;
  const spaceAbove = chipRect.top;
  const ddHeight = dd.offsetHeight || 320;
  dd.classList.toggle('flipped', (spaceBelow < ddHeight + 12) && (spaceAbove > spaceBelow));
}

function _toggleProfileDefaultWorkspaceDropdown(profileName){
  const dd = $('profileDefaultWorkspaceDropdown');
  const chip = $('profileDefaultWorkspaceChip');
  if (!dd || !chip) return;
  if (dd.classList.contains('open')) { closeWsDropdown(); return; }
  closeProfileDropdown();
  if (typeof closeModelDropdown === 'function') closeModelDropdown();
  if (typeof closeReasoningDropdown === 'function') closeReasoningDropdown();
  _closeProfileDefaultDropdowns();
  closeWsDropdown();
  const token = _profileRuntimeHydrationSeq;
  loadWorkspaceList().then(data => {
    if (!_isCurrentProfileRuntimeHydration(profileName, token)) return;
    const current = (_profileRuntimeSettings && _profileRuntimeSettings.default_workspace)
      || (chip.dataset.workspace || '');
    _applyProfileDefaultWorkspace(current);
    renderWorkspaceDropdownInto(dd, data.workspaces, current, {
      includeSessionActions: false,
      onSelect: (path) => {
        _markProfileRuntimeDirty('default_workspace');
        const prior = (_profileRuntimeSettings && _profileRuntimeSettings.default_workspace) || '';
        const next = String(path || '').trim();
        closeWsDropdown();
        _applyProfileDefaultWorkspace(next);
        _persistProfileDefaultWorkspace(profileName, next, prior).catch(()=>{});
      },
    });
    dd.classList.add('open');
    chip.classList.add('active');
    _positionProfileDefaultWorkspaceDropdown(chip, dd);
  });
}

async function _persistProfileDefaultWorkspace(profileName, next, prior){
  const token = _profileRuntimeHydrationSeq;
  const updated = await _persistProfileSetting(
    profileName,
    { default_workspace: String(next || '').trim() },
    () => {
      if (_isCurrentProfileRuntimeHydration(profileName, token)) _applyProfileDefaultWorkspace(prior || '');
    }
  );
  const saved = updated && typeof updated.default_workspace === 'string'
    ? updated.default_workspace
    : String(next || '').trim();
  if (_isCurrentProfileRuntimeHydration(profileName, token)) _applyProfileDefaultWorkspace(saved);
  const active = S.activeProfile || (_profilesCache && _profilesCache.active) || 'default';
  if (active === profileName) {
    S._profileDefaultWorkspace = saved;
    if (typeof syncWorkspaceDisplays === 'function') syncWorkspaceDisplays();
  }
  return updated;
}

function _profileCompressionPayload(){
  const current = _profileCompressionState();
  const threshold = $('profileCompressionThreshold');
  const protect = $('profileCompressionProtectLast');
  return Object.assign({}, current, {
    enabled: true,
    threshold: (Math.max(10, Math.min(95, parseInt(threshold && threshold.value || '50', 10) || 50)) / 100),
    protect_last_n: Math.max(0, Math.min(200, parseInt(protect && protect.value || '20', 10) || 0)),
  });
}

function _selectedProfileToolsets(){
  return Array.from(document.querySelectorAll('.profile-toolset-pill.active[data-toolset]'))
    .map(btn => btn.dataset.toolset)
    .filter(Boolean);
}

function _profileProviderForPickerValue(sel, modelValue, modelState){
  const stateProvider = modelState && modelState.model_provider ? String(modelState.model_provider).trim() : '';
  if (stateProvider) return stateProvider;
  const value = String(modelValue || '').trim();
  const slashProvider = value.includes('/') ? value.split('/')[0].trim() : '';
  if (!slashProvider || !sel) return '';
  for (const group of Array.from(sel.querySelectorAll('optgroup[data-provider]'))) {
    if (String(group.dataset.provider || '').trim() === slashProvider) return slashProvider;
  }
  return '';
}

function _wireProfileRuntimeSettingHandlers(profileName){
  const fallbackClear = $('profileFallbackClear');
  if (fallbackClear) fallbackClear.onclick = async (ev) => {
    ev.stopPropagation();
    _markProfileRuntimeDirty('fallback_model');
    const chip = $('profileFallbackModelChip');
    const sel = $('profileFallbackModelSelect');
    const priors = {
      selValue: sel ? (sel.value || '') : '',
      modelValue: chip ? (chip.dataset.modelValue || '') : '',
    };
    if (sel) sel.value = '';
    _applyProfileFallbackModelChip('');
    await _persistProfileFallbackModel(profileName, priors);
  };

  const auxBtn = $('profileAuxModelsButton');
  if (auxBtn) auxBtn.onclick = () => _openProfileAuxModels(profileName);

  const response = $('profileResponseModeSelect');
  if (response) response.onchange = () => {
    _markProfileRuntimeDirty('response_mode');
    const prior = (_profileRuntimeSettings && _profileRuntimeSettings.response_mode) || '';
    _persistProfileSetting(profileName, { response_mode: response.value }, () => _applyProfileResponseMode(prior)).catch(()=>{});
  };

  [$('profileCompressionThreshold'), $('profileCompressionProtectLast')]
    .filter(Boolean)
    .forEach(el => {
      el.oninput = () => {
        _markProfileRuntimeDirty('compression');
        _syncProfileCompressionSummary();
      };
      el.onchange = () => {
        _markProfileRuntimeDirty('compression');
        _syncProfileCompressionSummary();
        const prior = (_profileRuntimeSettings && _profileRuntimeSettings.compression) || {};
        _persistProfileSetting(profileName, { compression: _profileCompressionPayload() }, () => _applyProfileCompression(prior)).catch(()=>{});
      };
    });

  const maxSlider = $('profileMaxTurnsSlider');
  const maxInput = $('profileMaxTurnsInput');
  const saveMaxTurns = (value, prior) => {
    _markProfileRuntimeDirty('max_turns');
    const n = Math.max(1, Math.min(1000, parseInt(value || '150', 10) || 150));
    _applyProfileMaxTurns(n);
    _persistProfileSetting(profileName, { max_turns: n }, () => _applyProfileMaxTurns(prior)).catch(()=>{});
  };
  if (maxSlider) {
    maxSlider.oninput = () => {
      _markProfileRuntimeDirty('max_turns');
      if (maxInput) maxInput.value = maxSlider.value;
    };
    maxSlider.onchange = () => saveMaxTurns(maxSlider.value, _profileRuntimeSettings && _profileRuntimeSettings.max_turns);
  }
  if (maxInput) maxInput.onchange = () => saveMaxTurns(maxInput.value, _profileRuntimeSettings && _profileRuntimeSettings.max_turns);

  const workspaceChip = $('profileDefaultWorkspaceChip');
  if (workspaceChip) {
    workspaceChip.onclick = (ev) => {
      ev.stopPropagation();
      _toggleProfileDefaultWorkspaceDropdown(profileName);
    };
  }
  const workspaceClear = $('profileDefaultWorkspaceClear');
  if (workspaceClear) workspaceClear.onclick = async (ev) => {
    ev.stopPropagation();
    _markProfileRuntimeDirty('default_workspace');
    const current = (_profileRuntimeSettings && _profileRuntimeSettings.default_workspace) || '';
    _applyProfileDefaultWorkspace('');
    closeWsDropdown();
    _persistProfileDefaultWorkspace(profileName, '', current).catch(()=>{});
  };

  document.querySelectorAll('.profile-toolset-pill[data-toolset]').forEach(btn => {
    btn.onclick = () => {
      _markProfileRuntimeDirty('toolsets');
      btn.classList.toggle('active');
      btn.setAttribute('aria-pressed', btn.classList.contains('active') ? 'true' : 'false');
      const prior = (_profileRuntimeSettings && _profileRuntimeSettings.toolsets) || [];
      const priorConfigured = !!(_profileRuntimeSettings && _profileRuntimeSettings.toolsets_configured);
      _persistProfileSetting(profileName, { toolsets: _selectedProfileToolsets() }, () => _applyProfileToolsets(prior, priorConfigured)).catch(()=>{});
    };
  });
}

function _profileAuxiliaryModels(){
  const models = _profileRuntimeSettings && Array.isArray(_profileRuntimeSettings.auxiliary_models)
    ? _profileRuntimeSettings.auxiliary_models
    : [];
  return models.length ? models : [
    { task: 'vision', label: 'Vision', description: 'Image and multimodal interpretation.', provider: '', model: '' },
    { task: 'web_extract', label: 'Web extraction', description: 'Extract and summarize web page content.', provider: '', model: '' },
    { task: 'compression', label: 'Compression', description: 'Summarize long session context.', provider: '', model: '' },
    { task: 'session_search', label: 'Session search', description: 'Search and synthesize prior sessions.', provider: '', model: '' },
    { task: 'skills_hub', label: 'Skills hub', description: 'Skill discovery and routing support.', provider: '', model: '' },
    { task: 'approval', label: 'Approval', description: 'Policy and approval helper calls.', provider: '', model: '' },
    { task: 'mcp', label: 'MCP', description: 'MCP tool routing helper calls.', provider: '', model: '' },
    { task: 'title_generation', label: 'Title generation', description: 'Short chat and session titles.', provider: '', model: '' },
    { task: 'triage_specifier', label: 'Triage specifier', description: 'Clarify routing and task specification.', provider: '', model: '' },
    { task: 'curator', label: 'Curator', description: 'Curated summaries and organization.', provider: '', model: '' },
  ];
}

function _profileAuxDomId(task, suffix){
  return 'profileAux_' + String(task || '').replace(/[^a-z0-9_-]/gi, '_') + '_' + suffix;
}

function _openProfileAuxModels(profileName){
  _closeProfileAuxModels();
  const tasks = _profileAuxiliaryModels();
  const rows = tasks.map(item => {
    const task = esc(item.task || '');
    const label = esc(item.label || item.task || '');
    const desc = esc(item.description || '');
    const chipId = _profileAuxDomId(item.task, 'chip');
    const labelId = _profileAuxDomId(item.task, 'label');
    const ddId = _profileAuxDomId(item.task, 'dropdown');
    const selectId = _profileAuxDomId(item.task, 'select');
    return `
      <div class="profile-aux-model-row" data-aux-task="${task}">
        <div class="profile-aux-model-text">
          <span class="profile-aux-model-name">${label}</span>
          <span class="profile-aux-model-desc">${desc}</span>
        </div>
        <div class="profile-aux-model-control">
          <div class="composer-model-wrap profile-default-model-wrap profile-aux-model-wrap">
            <button class="composer-model-chip profile-aux-model-chip" id="${chipId}" type="button" title="Auxiliary model for ${label}">
              <span class="composer-model-icon" aria-hidden="true">${li('cpu',14)}</span>
              <span class="composer-model-label" id="${labelId}">Auto</span>
              <span class="composer-model-chevron" aria-hidden="true">${li('chevron-down',10)}</span>
            </button>
            <div class="model-dropdown profile-default-model-dropdown profile-aux-model-dropdown" id="${ddId}"></div>
            <select id="${selectId}" aria-hidden="true" tabindex="-1" style="position:absolute;width:1px;height:1px;clip:rect(0 0 0 0);overflow:hidden;"></select>
          </div>
          <button type="button" class="profile-runtime-icon-btn profile-aux-clear" data-aux-clear="${task}" title="Clear auxiliary model" aria-label="Clear ${label} auxiliary model">${li('x',14)}</button>
        </div>
      </div>`;
  }).join('');
  const overlay = document.createElement('div');
  overlay.id = 'profileAuxModelsOverlay';
  overlay.className = 'profile-skills-manager-overlay';
  overlay.innerHTML = `
    <div class="profile-skills-manager-card profile-aux-models-card" role="dialog" aria-modal="true" aria-labelledby="profileAuxModelsTitle">
      <div class="profile-skills-manager-head">
        <div class="profile-skills-manager-head-text">
          <div class="profile-skills-manager-kicker">Runtime</div>
          <h3 class="profile-skills-manager-title" id="profileAuxModelsTitle">Auxiliary tool models</h3>
          <div class="profile-skills-manager-subtitle">Set cheaper or specialized models for profile helper tasks.</div>
        </div>
        <button type="button" class="profile-skills-manager-close" data-aux-close aria-label="Close">×</button>
      </div>
      <div class="profile-skills-manager-body profile-aux-models-body">
        ${rows}
      </div>
      <div class="profile-skills-manager-footer">
        <span class="profile-skills-manager-note">Clear a row to let Hermes resolve that auxiliary task from its normal runtime defaults.</span>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', (ev) => {
    if (ev.target === overlay) _closeProfileAuxModels();
  });
  const closeBtn = overlay.querySelector('[data-aux-close]');
  if (closeBtn) closeBtn.onclick = _closeProfileAuxModels;
  tasks.forEach(item => {
    const selectId = _profileAuxDomId(item.task, 'select');
    _populateProfileModelSelect(selectId, item.model || '', item.provider || null);
    _applyProfileAuxModelChip(item.task, item.model || '');
  });
  _wireProfileAuxModelHandlers(profileName);
}

function _closeProfileAuxModels(){
  const overlay = $('profileAuxModelsOverlay');
  if (overlay) overlay.remove();
}

function _closeProfileAuxDropdowns(){
  document.querySelectorAll('.profile-aux-model-dropdown.open').forEach(el => {
    el.classList.remove('open');
    el.classList.remove('flipped');
  });
  document.querySelectorAll('.profile-aux-model-chip.active').forEach(el => el.classList.remove('active'));
}

function _findProfileAuxModel(task){
  return _profileAuxiliaryModels().find(item => item.task === task) || { task, provider: '', model: '' };
}

function _applyProfileAuxModelChip(task, modelValue){
  const label = $(_profileAuxDomId(task, 'label'));
  const chip = $(_profileAuxDomId(task, 'chip'));
  const clear = document.querySelector(`[data-aux-clear="${CSS.escape(task)}"]`);
  const text = modelValue
    ? (typeof getModelLabel === 'function' ? getModelLabel(modelValue) : modelValue)
    : 'Auto';
  if (label) label.textContent = text;
  if (chip) {
    chip.dataset.modelValue = modelValue || '';
    chip.classList.toggle('inactive', !modelValue);
    chip.title = modelValue ? ('Auxiliary model: ' + text) : 'Use Hermes default for this auxiliary task';
  }
  if (clear) clear.disabled = !modelValue;
}

function _wireProfileAuxModelHandlers(profileName){
  _profileAuxiliaryModels().forEach(item => {
    const task = item.task;
    const chip = $(_profileAuxDomId(task, 'chip'));
    if (chip) {
      chip.onclick = (ev) => {
        ev.stopPropagation();
        _toggleProfileAuxModelDropdown(profileName, task);
      };
    }
  });
  document.querySelectorAll('[data-aux-clear]').forEach(btn => {
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      _markProfileRuntimeDirty('auxiliary_models');
      const task = btn.dataset.auxClear || '';
      const chip = $(_profileAuxDomId(task, 'chip'));
      const sel = $(_profileAuxDomId(task, 'select'));
      const priors = {
        selValue: sel ? (sel.value || '') : '',
        modelValue: chip ? (chip.dataset.modelValue || '') : '',
      };
      if (sel) sel.value = '';
      _applyProfileAuxModelChip(task, '');
      await _persistProfileAuxModel(profileName, task, '', priors);
    };
  });
}

function _toggleProfileAuxModelDropdown(profileName, task){
  const dd = $(_profileAuxDomId(task, 'dropdown'));
  const chip = $(_profileAuxDomId(task, 'chip'));
  const sel = $(_profileAuxDomId(task, 'select'));
  if (!dd || !chip || !sel) return;
  if (dd.classList.contains('open')) { _closeProfileAuxDropdowns(); return; }
  if (typeof closeModelDropdown === 'function') closeModelDropdown();
  if (typeof closeReasoningDropdown === 'function') closeReasoningDropdown();
  _closeProfileDefaultDropdowns();
  _closeProfileAuxDropdowns();
  renderModelDropdown({
    select: sel,
    dropdown: dd,
    onSelect: (value) => _onProfileAuxModelPicked(profileName, task, value),
    onClose: () => _closeProfileAuxDropdowns(),
    scopeNote: "",
  });
  dd.classList.add('open');
  chip.classList.add('active');
  _positionProfileDefaultDropdown(chip, dd);
}

async function _onProfileAuxModelPicked(profileName, task, value){
  _markProfileRuntimeDirty('auxiliary_models');
  const sel = $(_profileAuxDomId(task, 'select'));
  const chip = $(_profileAuxDomId(task, 'chip'));
  if (!sel) { _closeProfileAuxDropdowns(); return; }
  const priors = {
    selValue: sel.value || '',
    modelValue: chip ? (chip.dataset.modelValue || '') : '',
  };
  if (!Array.from(sel.options).some(o => o.value === value)) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = getModelLabel(value);
    opt.dataset.custom = '1';
    sel.querySelectorAll('option[data-custom]').forEach(o => o.remove());
    sel.appendChild(opt);
  }
  sel.value = value;
  _applyProfileAuxModelChip(task, value);
  _closeProfileAuxDropdowns();
  await _persistProfileAuxModel(profileName, task, value, priors);
}

async function _persistProfileAuxModel(profileName, task, modelValue, priors){
  const token = _profileRuntimeHydrationSeq;
  const sel = $(_profileAuxDomId(task, 'select'));
  const priorModel = (priors && typeof priors.modelValue === 'string') ? priors.modelValue : '';
  const priorSelValue = (priors && typeof priors.selValue === 'string') ? priors.selValue : '';
  const modelState = (sel && modelValue && typeof _modelStateForSelect === 'function')
    ? _modelStateForSelect(sel, modelValue)
    : { model: modelValue || '', model_provider: null };
  const provider = _profileProviderForPickerValue(sel, modelValue, modelState);
  if (modelValue && !provider) {
    if (sel) {
      if (priorSelValue && Array.from(sel.options).some(o => o.value === priorSelValue)) sel.value = priorSelValue;
      else sel.value = '';
    }
    _applyProfileAuxModelChip(task, priorModel);
    showToast('Pick an auxiliary model from a configured provider.', 4000, 'error');
    return;
  }
  const patch = {};
  patch[task] = modelValue ? {
    provider,
    model: modelState.model || modelValue,
  } : { provider: '', model: '' };
  try {
    const updated = await _persistProfileSetting(profileName, { auxiliary_models: patch });
    if (_isCurrentProfileRuntimeHydration(profileName, token) && updated && Array.isArray(updated.auxiliary_models)) {
      updated.auxiliary_models.forEach(item => _applyProfileAuxModelChip(item.task, item.model || ''));
    }
  } catch (_) {
    if (_isCurrentProfileRuntimeHydration(profileName, token)) {
      if (sel) {
        if (priorSelValue && Array.from(sel.options).some(o => o.value === priorSelValue)) sel.value = priorSelValue;
        else sel.value = '';
      }
      _applyProfileAuxModelChip(task, priorModel);
    }
  }
}

// Click-outside / Escape closes the default-model dropdowns.
document.addEventListener('click', (ev) => {
  if (!ev.target.closest('.profile-default-model-wrap')) _closeProfileDefaultDropdowns();
  if (!ev.target.closest('.profile-aux-model-wrap')) _closeProfileAuxDropdowns();
});
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') _closeProfileDefaultDropdowns();
  if (ev.key === 'Escape') {
    _closeProfileAuxDropdowns();
    if ($('profileAuxModelsOverlay')) _closeProfileAuxModels();
  }
});

// ── Skills tile — top-3 enabled chips ──────────────────────────────────

const _profileSkillsCache = Object.create(null);

async function _loadProfileSkillsTile(profile){
  // Accept either a profile object {name:...} or a plain name string.
  const profileName = profile && typeof profile === 'object' ? profile.name : profile;
  if (!profileName) return;
  let data;
  try {
    data = await api('/api/profile/skills?name=' + encodeURIComponent(profileName));
  } catch (e) {
    // Backend unreachable / agent missing — leave the initial render in place.
    return;
  }
  _profileSkillsCache[profileName] = data;
  // Only repaint if the user is still on this profile.
  if (!_currentProfileDetail || _currentProfileDetail.name !== profileName) return;
  _applyProfileSkillsSummary(data);
}

function _applyProfileSkillsSummary(data){
  if (!data) return;
  const enabled_count = data.enabled_count || 0;
  const total_count = data.total_count || 0;
  const dot = $('opsSkillsDot');
  const pillLabel = $('opsSkillsPillLabel');
  const value = $('opsSkillsValue');
  const chips = $('opsSkillsTopChips');
  if (dot) { dot.classList.remove('ok','off'); dot.classList.add(enabled_count > 0 ? 'ok' : 'off'); }
  if (pillLabel) pillLabel.textContent = `${enabled_count} / ${total_count} enabled`;
  if (value) {
    if (total_count === 0) value.textContent = 'No skills installed';
    else if (enabled_count === 0) value.textContent = 'No skills enabled';
    else value.textContent = 'Top in this profile';
  }
  if (chips) {
    const sample = (data.skills || [])
      .filter(s => s.enabled)
      .slice(0, 3);
    if (sample.length && enabled_count > 0) {
      const extra = enabled_count - sample.length;
      const parts = sample.map(s => {
        const label = esc(s.label || s.name || 'skill');
        const icon = s.icon ? esc(s.icon) + ' ' : '';
        return `<span class="profile-skill-chip" role="listitem">${icon}${label}</span>`;
      });
      if (extra > 0) parts.push(`<span class="profile-skill-more">+${extra} more</span>`);
      chips.innerHTML = parts.join('');
    } else {
      chips.innerHTML = '';
    }
  }
}

// ── Per-profile Skills manager modal ─────────────────────────────────────
//
// Opened from the Ops Console "Manage" button. Lists every skill visible
// to the profile with a toggle per row; the Edit button opens a markdown
// editor for the shared skill content (writes back to the same file the
// agent reads at runtime).

let _skillsManagerProfile = null;
let _skillsManagerFilter = '';

// Pending-state model for the batched Save/Cancel UX.
//
// The modal holds a draft disabled-set in memory. Toggling a row mutates
// _skillsManagerPendingDisabled only — no network call until Save. This keeps
// the disk write to a single POST per editing session and gives the user a
// clear "I'm done experimenting" commit boundary.
let _skillsManagerSkills = [];          // [{name, description, category, source, path}]
let _skillsManagerInitialDisabled = null;   // Set<string> — server state at open
let _skillsManagerPendingDisabled = null;   // Set<string> — what Save will write
let _skillsManagerCategories = [];
let _skillsManagerSaving = false;

function _skillsManagerIsDirty(){
  if (!_skillsManagerInitialDisabled || !_skillsManagerPendingDisabled) return false;
  if (_skillsManagerInitialDisabled.size !== _skillsManagerPendingDisabled.size) return true;
  for (const v of _skillsManagerPendingDisabled) {
    if (!_skillsManagerInitialDisabled.has(v)) return true;
  }
  return false;
}

function _skillsManagerPendingDelta(){
  // Returns {willDisable, willEnable} arrays of skill names.
  const a = _skillsManagerInitialDisabled || new Set();
  const b = _skillsManagerPendingDisabled || new Set();
  const willDisable = [];
  const willEnable = [];
  for (const v of b) if (!a.has(v)) willDisable.push(v);
  for (const v of a) if (!b.has(v)) willEnable.push(v);
  return { willDisable, willEnable };
}

async function _openProfileSkillsManager(profileName){
  if (!profileName) return;
  _skillsManagerProfile = profileName;
  _skillsManagerFilter = '';
  _skillsManagerSkills = [];
  _skillsManagerInitialDisabled = null;
  _skillsManagerPendingDisabled = null;
  _skillsManagerCategories = [];
  _skillsManagerSaving = false;
  // Reuse the cached payload from the tile hydrator when available so the
  // modal pops instantly; refresh in the background regardless.
  const cached = _profileSkillsCache[profileName];
  _renderSkillsManagerModal(cached || { ok: true, profile: profileName, skills: [], enabled_count: 0, total_count: 0 }, !cached);
  try {
    const fresh = await api('/api/profile/skills?name=' + encodeURIComponent(profileName));
    _profileSkillsCache[profileName] = fresh;
    if (_skillsManagerProfile === profileName) _renderSkillsManagerModal(fresh, false);
    if (_currentProfileDetail && _currentProfileDetail.name === profileName) {
      _applyProfileSkillsSummary(fresh);
    }
  } catch (e) {
    if (_skillsManagerProfile === profileName) {
      const body = $('profileSkillsManagerBody');
      if (body) body.innerHTML = `<div class="profile-skills-error">Failed to load skills: ${esc(e.message || String(e))}</div>`;
    }
  }
}

async function _attemptCloseSkillsManager(){
  if (_skillsManagerSaving) return;
  if (_skillsManagerIsDirty()) {
    const ok = await showConfirmDialog({
      title: 'Discard pending changes?',
      message: 'You have unsaved toggle changes. Closing now will discard them.',
      confirmLabel: 'Discard',
      cancelLabel: 'Keep editing',
      danger: true,
      focusCancel: true,
    });
    if (!ok) return;
  }
  _closeProfileSkillsManager();
}

function _closeProfileSkillsManager(){
  _skillsManagerProfile = null;
  _skillsManagerSkills = [];
  _skillsManagerInitialDisabled = null;
  _skillsManagerPendingDisabled = null;
  _skillsManagerSaving = false;
  const overlay = $('profileSkillsManagerOverlay');
  if (overlay) overlay.remove();
  document.removeEventListener('keydown', _skillsManagerEscape, true);
}

function _skillsManagerEscape(ev){
  if (ev.key === 'Escape') _attemptCloseSkillsManager();
}

function _renderSkillsManagerModal(data, loading){
  let overlay = $('profileSkillsManagerOverlay');
  const firstRender = !overlay;
  if (firstRender) {
    overlay = document.createElement('div');
    overlay.id = 'profileSkillsManagerOverlay';
    overlay.className = 'profile-skills-manager-overlay';
    overlay.innerHTML = `
      <div class="profile-skills-manager-card" role="dialog" aria-modal="true" aria-labelledby="profileSkillsManagerTitle">
        <div class="profile-skills-manager-head">
          <div class="profile-skills-manager-head-text">
            <div class="profile-skills-manager-kicker">Skills</div>
            <h3 class="profile-skills-manager-title" id="profileSkillsManagerTitle">Skills for <span id="profileSkillsManagerName"></span></h3>
            <div class="profile-skills-manager-subtitle" id="profileSkillsManagerSummary"></div>
          </div>
          <div class="profile-skills-manager-actions">
            <span id="profileSkillsManagerDirtyPill" class="profile-ops-status-pill" data-state="saved" aria-live="polite">
              <span class="profile-status-dot ok" aria-hidden="true"></span>
              <span id="profileSkillsManagerDirtyLabel">No changes</span>
            </span>
            <button type="button" class="profile-ops-button primary" data-skills-manager-save disabled>Save</button>
            <button type="button" class="profile-skills-manager-close" aria-label="Close">&times;</button>
          </div>
        </div>
        <div class="profile-skills-manager-toolbar">
          <input type="search" id="profileSkillsManagerSearch" placeholder="Filter by name, description, or category…" autocomplete="off" spellcheck="false">
        </div>
        <div id="profileSkillsManagerBody" class="profile-skills-manager-body"></div>
        <div class="profile-skills-manager-footer">
          <span class="profile-skills-manager-note">Toggling enabled/disabled is scoped to this agent. Editing a skill changes content shared by every agent that uses it.</span>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector('.profile-skills-manager-close').onclick = _attemptCloseSkillsManager;
    overlay.querySelector('[data-skills-manager-save]').onclick = _saveSkillsManager;
    overlay.addEventListener('click', (ev) => {
      if (ev.target === overlay) _attemptCloseSkillsManager();
    });
    const search = overlay.querySelector('#profileSkillsManagerSearch');
    if (search) {
      search.value = _skillsManagerFilter;
      search.addEventListener('input', () => {
        _skillsManagerFilter = search.value || '';
        _paintSkillsManagerRows();
      });
    }
    document.addEventListener('keydown', _skillsManagerEscape, true);
  }
  const nameEl = $('profileSkillsManagerName');
  if (nameEl) nameEl.textContent = data.profile || _skillsManagerProfile || '';
  // Seed pending-state on first render (or when we still have no state),
  // then prefer the in-memory pending set across subsequent refreshes so
  // the user's mid-edit toggles survive a background reload.
  const all = Array.isArray(data && data.skills) ? data.skills : [];
  _skillsManagerSkills = all.map(s => ({
    name: s.name,
    description: s.description,
    category: s.category,
    source: s.source,
    path: s.path,
  }));
  _skillsManagerCategories = Array.isArray(data && data.categories) ? data.categories : [];
  if (_skillsManagerInitialDisabled === null) {
    const seed = new Set(all.filter(s => !s.enabled).map(s => s.name));
    _skillsManagerInitialDisabled = seed;
    _skillsManagerPendingDisabled = new Set(seed);
  } else {
    // Drop any pending entries for skills that no longer exist server-side.
    const known = new Set(all.map(s => s.name));
    for (const v of [..._skillsManagerPendingDisabled]) {
      if (!known.has(v)) _skillsManagerPendingDisabled.delete(v);
    }
  }
  const body = $('profileSkillsManagerBody');
  if (body && loading) body.innerHTML = '<div class="profile-skills-loading">Loading skills…</div>';
  _paintSkillsManagerRows();
  _paintSkillsManagerFooter();
}

function _paintSkillsManagerFooter(){
  const total = _skillsManagerSkills.length;
  const pendingDisabled = _skillsManagerPendingDisabled || new Set();
  const enabledNow = total - pendingDisabled.size;
  const summary = $('profileSkillsManagerSummary');
  if (summary) {
    summary.textContent = total === 0
      ? 'No skills visible to this agent yet.'
      : `${enabledNow} of ${total} enabled${_skillsManagerCategories.length
          ? ` · ${_skillsManagerCategories.length} categor${_skillsManagerCategories.length === 1 ? 'y' : 'ies'}`
          : ''}`;
  }
  const dirty = _skillsManagerIsDirty();
  const pill = $('profileSkillsManagerDirtyPill');
  const label = $('profileSkillsManagerDirtyLabel');
  const dot = pill ? pill.querySelector('.profile-status-dot') : null;
  if (pill) pill.dataset.state = dirty ? 'dirty' : 'saved';
  if (dot) { dot.classList.remove('ok','warn'); dot.classList.add(dirty ? 'warn' : 'ok'); }
  if (label) {
    if (!dirty) {
      label.textContent = 'No changes';
    } else {
      const { willDisable, willEnable } = _skillsManagerPendingDelta();
      const n = willDisable.length + willEnable.length;
      label.textContent = `${n} change${n === 1 ? '' : 's'} pending`;
    }
  }
  const overlay = $('profileSkillsManagerOverlay');
  const save = overlay ? overlay.querySelector('[data-skills-manager-save]') : null;
  if (save) save.disabled = !dirty || _skillsManagerSaving;
}

function _paintSkillsManagerRows(){
  const body = $('profileSkillsManagerBody');
  if (!body) return;
  const all = _skillsManagerSkills;
  if (all.length === 0) {
    body.innerHTML = `<div class="profile-skills-empty">
      <p>No skills are visible to this agent yet.</p>
      <p class="profile-skills-empty-hint">Drop SKILL.md folders into this profile's <code>skills/</code> directory, or configure <code>skills.external_dirs</code> in its <code>config.yaml</code>.</p>
    </div>`;
    return;
  }
  const filter = (_skillsManagerFilter || '').trim().toLowerCase();
  const rows = filter
    ? all.filter(s =>
        (s.name || '').toLowerCase().includes(filter) ||
        (s.description || '').toLowerCase().includes(filter) ||
        (s.category || '').toLowerCase().includes(filter))
    : all;
  if (rows.length === 0) {
    body.innerHTML = `<div class="profile-skills-empty">No skills match "${esc(filter)}".</div>`;
    return;
  }
  const initial = _skillsManagerInitialDisabled || new Set();
  const pending = _skillsManagerPendingDisabled || new Set();
  // Group by category for readability.
  const groups = new Map();
  for (const r of rows) {
    const key = r.category || 'Uncategorized';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  const groupKeys = Array.from(groups.keys()).sort((a, b) => a.localeCompare(b));
  const sections = groupKeys.map(cat => {
    const items = groups.get(cat).map(s => {
      const isEnabledPending = !pending.has(s.name);
      const wasEnabledInitial = !initial.has(s.name);
      const isDirty = isEnabledPending !== wasEnabledInitial;
      const checked = isEnabledPending ? 'checked' : '';
      const desc = s.description ? `<span class="profile-skill-row-desc">${esc(s.description)}</span>` : '';
      const pendingChip = isDirty
        ? `<span class="profile-skill-row-pending" title="Unsaved change">${isEnabledPending ? 'will enable' : 'will disable'}</span>`
        : '';
      return `
        <div class="profile-skill-row${isDirty ? ' is-pending' : ''}" data-skill-name="${esc(s.name)}">
          <label class="profile-skill-row-toggle" title="Enable / disable for this agent">
            <input type="checkbox" data-skill-toggle="${esc(s.name)}" ${checked}>
            <span class="profile-skill-row-switch" aria-hidden="true"></span>
          </label>
          <div class="profile-skill-row-text">
            <span class="profile-skill-row-name">${esc(s.name)}${pendingChip}</span>
            ${desc}
          </div>
          <button type="button" class="profile-ops-button profile-skill-row-edit" data-skill-edit="${esc(s.name)}">Edit</button>
        </div>`;
    }).join('');
    return `
      <section class="profile-skill-group">
        <h4 class="profile-skill-group-title">${esc(cat)}</h4>
        <div class="profile-skill-group-list">${items}</div>
      </section>`;
  });
  body.innerHTML = sections.join('');
  // Wire toggles and edit buttons.
  body.querySelectorAll('[data-skill-toggle]').forEach(input => {
    input.addEventListener('change', () => _toggleProfileSkill(input.dataset.skillToggle, input.checked));
  });
  body.querySelectorAll('[data-skill-edit]').forEach(btn => {
    btn.addEventListener('click', () => _editProfileSkillContent(btn.dataset.skillEdit));
  });
}

function _toggleProfileSkill(skillName, enabled){
  if (!skillName || !_skillsManagerPendingDisabled) return;
  if (enabled) _skillsManagerPendingDisabled.delete(skillName);
  else _skillsManagerPendingDisabled.add(skillName);
  _paintSkillsManagerRows();
  _paintSkillsManagerFooter();
}

async function _saveSkillsManager(){
  if (!_skillsManagerProfile || !_skillsManagerPendingDisabled) return;
  if (!_skillsManagerIsDirty()) return;
  const profileName = _skillsManagerProfile;
  const disabled = Array.from(_skillsManagerPendingDisabled).sort();
  _skillsManagerSaving = true;
  _paintSkillsManagerFooter();
  try {
    const result = await api('/api/profile/skills', {
      method: 'POST',
      body: JSON.stringify({ name: profileName, disabled }),
    });
    _profileSkillsCache[profileName] = result;
    if (_currentProfileDetail && _currentProfileDetail.name === profileName) {
      _applyProfileSkillsSummary(result);
    }
    _skillsManagerSaving = false;
    // Saved → clear pending state so close doesn't prompt.
    _skillsManagerInitialDisabled = null;
    _skillsManagerPendingDisabled = null;
    _closeProfileSkillsManager();
    showToast('Skills saved.');
  } catch (e) {
    _skillsManagerSaving = false;
    _paintSkillsManagerFooter();
    showToast('Failed to save skills: ' + (e.message || e));
  }
}

async function _editProfileSkillContent(skillName){
  const profileName = _skillsManagerProfile;
  if (!profileName || !skillName) return;
  let payload;
  try {
    payload = await api('/api/profile/skill_content?name=' + encodeURIComponent(profileName) +
                        '&skill=' + encodeURIComponent(skillName));
  } catch (e) {
    showToast('Could not open skill: ' + (e.message || e));
    return;
  }
  _closeProfileSkillsManager();
  _openSkillContentEditor(profileName, skillName, payload && payload.content != null ? payload.content : '');
}

function _openSkillContentEditor(profileName, skillName, content){
  const body = $('profileDetailBody');
  const title = $('profileDetailTitle');
  if (!body || !title) return;
  title.innerHTML = `<span style="cursor:pointer;color:var(--link)" onclick="openProfileDetail('${esc(profileName)}')">${esc(profileName)}</span> / skill · ${esc(skillName)}`;
  body.innerHTML = `
    <div class="main-view-content">
      <div class="detail-card profile-file-editor-card">
        <div class="profile-file-editor-toolbar">
          <button class="profile-ops-button" type="button" data-skill-editor-back>← Back to skills</button>
          <div class="profile-file-editor-titlebar">
            <span class="profile-file-editor-name">${esc(skillName)}</span>
            <span class="profile-file-editor-meta">shared skill content · markdown</span>
          </div>
          <span id="profileSkillSaveState" class="profile-ops-status-pill" data-state="saved"><span class="profile-status-dot ok" aria-hidden="true"></span><span>Saved</span></span>
          <button id="btnSaveProfileSkill" class="profile-ops-button primary" type="button">Save</button>
        </div>
        <textarea id="profileSkillEditor" rows="24" spellcheck="false"
          class="profile-file-editor-textarea"
          placeholder="# Skill name&#10;Markdown body — what this skill does, when to invoke it, and any required state."></textarea>
      </div>
    </div>`;
  const ta = $('profileSkillEditor');
  if (ta) ta.value = content || '';
  const pill = $('profileSkillSaveState');
  if (ta && pill) {
    ta.addEventListener('input', () => {
      if (pill.dataset.state === 'dirty') return;
      pill.dataset.state = 'dirty';
      const dot = pill.querySelector('.profile-status-dot');
      const lbl = pill.querySelector('span:last-child');
      if (dot) { dot.classList.remove('ok'); dot.classList.add('warn'); }
      if (lbl) lbl.textContent = 'Unsaved';
    });
  }
  const back = body.querySelector('[data-skill-editor-back]');
  if (back) back.onclick = () => {
    openProfileDetail(profileName);
    // Reopen the manager once the detail re-renders.
    setTimeout(() => _openProfileSkillsManager(profileName), 60);
  };
  const saveBtn = $('btnSaveProfileSkill');
  if (saveBtn) saveBtn.onclick = () => _saveProfileSkillContent(profileName, skillName);
}

async function _saveProfileSkillContent(profileName, skillName){
  const ta = $('profileSkillEditor');
  const btn = $('btnSaveProfileSkill');
  if (!ta) return;
  const content = ta.value;
  if (btn) btn.disabled = true;
  try {
    const result = await api('/api/profile/skill_content', {
      method: 'POST',
      body: JSON.stringify({ name: profileName, skill: skillName, content }),
    });
    if (result && result.ok) {
      showToast(`Saved ${skillName}`);
      const pill = $('profileSkillSaveState');
      if (pill) {
        pill.dataset.state = 'saved';
        const dot = pill.querySelector('.profile-status-dot');
        const lbl = pill.querySelector('span:last-child');
        if (dot) { dot.classList.remove('warn'); dot.classList.add('ok'); }
        if (lbl) lbl.textContent = 'Saved';
      }
    } else {
      showToast('Save failed: ' + (result && result.error || 'unknown error'));
    }
  } catch (e) {
    showToast('Save failed: ' + (e.message || e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

// _profileIdentityPlane: removed in profile screen rework v3 (2026-05-14).
// Replaced by _profileHeroDossier (256×256 avatar, inline action buttons, no
// overflow menu, no bare green diode next to the name).

// _profileOpsTiles: removed in profile screen rework v3 (2026-05-14).
// Split into three smaller renderers (_profileRuntimePanel,
// _profileGatewayTile, _profileSkillsTile). The Active and Ready tiles
// from v2 are dropped — Active is conveyed by the inline pill in the hero
// dossier, .env readiness by the file widget's status text.

function _profileFilesSection(p){
  const profileName = esc(p.name);
  // Status strings match the spec's Files-grid table exactly (validator
  // F#16): three Markdowns, env Configured/Not-configured · hidden, YAML.
  const envStatus = p.has_env ? 'Configured · hidden' : 'Not configured · hidden';
  const envClass = p.has_env ? '' : 'warn';
  // Lucide icons replace the single-letter badges from v2 so the files row
  // shares iconography with the rest of the webUI (icons.js).
  const files = [
    { name: 'SOUL.md',             icon: 'user',      label: 'SOUL.md',          desc: 'Persona & principles.',           status: 'Markdown', statusClass: '' },
    { name: 'memories/MEMORY.md',  icon: 'brain',     label: 'Memory',           desc: 'Long-lived notes.',               status: 'Markdown', statusClass: '' },
    { name: 'memories/USER.md',    icon: 'settings',  label: 'User preferences', desc: 'Taste & defaults.',               status: 'Markdown', statusClass: '' },
    { name: '.env',                icon: 'lock',      label: '.env',             desc: 'Provider credentials.',           status: envStatus,  statusClass: envClass },
    { name: 'config.yaml',         icon: 'file-code', label: 'config.yaml',      desc: 'Runtime defaults.',               status: 'YAML',     statusClass: '' },
  ];
  const widgets = files.map(f => `
    <button class="profile-file-widget" type="button" data-profile-file="${esc(f.name)}">
      <span class="profile-file-icon">${li(f.icon, 16)}</span>
      <span>
        <span class="profile-file-name">${esc(f.label)}</span>
        <span class="profile-file-desc">${esc(f.desc)}</span>
      </span>
      <span class="profile-file-status ${f.statusClass}">${esc(f.status)}</span>
    </button>`).join('');
  return `
    <section class="profile-ops-files-section" aria-labelledby="opsFilesTitle">
      <div class="profile-ops-files-head">
        <div>
          <div class="profile-section-label">Profile files</div>
          <h3 class="profile-ops-files-title" id="opsFilesTitle">Editable source files for ${profileName}</h3>
        </div>
      </div>
      <div class="profile-ops-files-grid">${widgets}</div>
    </section>`;
}

// Old v2 _profileFilesSection swallowed below; the call site uses the v3
// version above. The v2 body had the same shape but with single-letter icons.
function _profileOpsAvatarDialog(p){
  const profileName = esc(p.name);
  const slotRows = PROFILE_REACTIVE_AVATAR_SLOTS.map(slot => {
    const key = esc(slot.key);
    const inputId = `profileReactiveAvatar${key}`;
    return `
            <div class="profile-reactive-slot" data-reactive-slot="${key}">
              <div id="${inputId}Preview" class="profile-reactive-slot-preview"></div>
              <div class="profile-reactive-slot-copy">
                <div class="profile-reactive-slot-title">${esc(slot.label)}</div>
                <div class="profile-reactive-slot-hint">${esc(slot.hint)}</div>
                <div id="${inputId}Status" class="profile-reactive-slot-status">Using fallback</div>
              </div>
              <input id="${inputId}" class="profile-reactive-file-input" type="file" accept="image/png,image/jpeg,image/gif,image/webp" tabindex="-1" aria-hidden="true" onchange="_handleReactiveAvatarUpload('${key}',this.files&&this.files[0])">
              <div class="profile-reactive-slot-actions">
                <button type="button" class="profile-reactive-slot-upload" onclick="$('${inputId}')&&$('${inputId}').click()">Choose</button>
                <button type="button" class="profile-reactive-slot-clear" onclick="_clearReactiveAvatarSlot('${key}')">Clear</button>
              </div>
            </div>`;
  }).join('');
  const previewStates = PROFILE_REACTIVE_AVATAR_SLOTS.map(slot => `
              <button type="button" data-avatar-preview-state="${esc(slot.key)}" onclick="_setProfileAvatarPreviewState('${esc(slot.key)}')">${esc(slot.label)}</button>`).join('');
  return `
    <div id="profileAvatarDialog" class="profile-avatar-dialog" hidden>
      <div class="profile-avatar-dialog-card" role="dialog" aria-label="Change profile avatar">
        <div class="profile-avatar-dialog-head">
          <div>
            <div class="profile-avatar-dialog-title">Change avatar</div>
            <div class="profile-avatar-dialog-subtitle">Choose a static avatar or upload reactive animation slots for live chat.</div>
          </div>
          <button type="button" class="profile-avatar-dialog-close" onclick="_closeProfileAvatarDialog()" aria-label="Close">×</button>
        </div>
        <div class="profile-avatar-dialog-body profile-avatar-dialog-layout">
          <aside class="profile-avatar-preview-panel" aria-label="Avatar preview">
            <div class="profile-avatar-section-label">Live preview</div>
            <div class="profile-avatar-preview-frame">
              <div id="profileAvatarDialogPreview" class="profile-avatar-dialog-preview" data-profile-name="${profileName}">${_profileAvatarForUi(p,'profile-avatar--dialog')}</div>
            </div>
            <div id="profileAvatarPreviewStateControls" class="profile-avatar-preview-states" role="group" aria-label="Preview reactive state" hidden>
${previewStates}
            </div>
            <div id="profileAvatarPreviewSummary" class="profile-avatar-preview-summary">Static avatar preview. Reactive uploads stay saved when you switch modes.</div>
          </aside>
          <div class="profile-avatar-editor-panel">
            <section class="profile-avatar-editor-section" aria-label="Avatar behavior">
              <div class="profile-avatar-section-label">Behavior</div>
              <div class="profile-avatar-runtime-row profile-avatar-segmented" role="group" aria-label="Avatar behavior">
                <button type="button" class="profile-avatar-mode-card" data-avatar-runtime-mode="static" onclick="_setProfileAvatarRuntimeMode('static')">
                  <span class="profile-avatar-runtime-option-title">Static</span>
                  <span class="profile-avatar-runtime-option-copy">One image or emoji across every state.</span>
                </button>
                <button type="button" class="profile-avatar-mode-card" data-avatar-runtime-mode="reactive" onclick="_setProfileAvatarRuntimeMode('reactive')">
                  <span class="profile-avatar-runtime-option-title">Reactive</span>
                  <span class="profile-avatar-runtime-option-copy">Use animation slots for idle, thinking, talking, work, and errors.</span>
                </button>
              </div>
            </section>
            <section class="profile-avatar-editor-section" aria-label="Avatar frame">
              <div class="profile-avatar-section-label">Frame</div>
              <div class="profile-avatar-shape-row profile-avatar-mini-segment" role="group" aria-label="Avatar shape">
                <button type="button" data-avatar-shape="square" onclick="_setProfileAvatarDialogShape('square')">Square</button>
                <button type="button" data-avatar-shape="circle" onclick="_setProfileAvatarDialogShape('circle')">Circle</button>
              </div>
            </section>
            <section id="profileAvatarStaticEditor" class="profile-avatar-editor-section" aria-label="Static avatar editor">
              <div class="profile-avatar-section-label">Static source</div>
              <div class="profile-avatar-dialog-tabs profile-avatar-mini-segment" role="group" aria-label="Static avatar source">
                <button type="button" data-avatar-mode="emoji" onclick="_setProfileAvatarDialogMode('emoji')">Emoji</button>
                <button type="button" data-avatar-mode="upload" onclick="_setProfileAvatarDialogMode('upload')">Upload</button>
                <button type="button" data-avatar-mode="url" onclick="_setProfileAvatarDialogMode('url')">Image URL</button>
                <button type="button" data-avatar-mode="asset" onclick="_setProfileAvatarDialogMode('asset')">Asset</button>
              </div>
              <div data-profile-avatar-pane="emoji" class="profile-avatar-choice">
                <label for="profileAvatarEmoji">Emoji</label>
                <input id="profileAvatarEmoji" type="text" maxlength="64" placeholder="🤖" oninput="_refreshProfileAvatarDialogPreview()">
                <div class="profile-avatar-emoji-grid" aria-label="Quick emoji avatars">
                  <button type="button" onclick="_chooseProfileAvatarEmoji('🤖')">🤖</button>
                  <button type="button" onclick="_chooseProfileAvatarEmoji('🧠')">🧠</button>
                  <button type="button" onclick="_chooseProfileAvatarEmoji('🧙‍♂️')">🧙‍♂️</button>
                  <button type="button" onclick="_chooseProfileAvatarEmoji('🛡️')">🛡️</button>
                  <button type="button" onclick="_chooseProfileAvatarEmoji('🔬')">🔬</button>
                  <button type="button" onclick="_chooseProfileAvatarEmoji('✍️')">✍️</button>
                </div>
              </div>
              <div data-profile-avatar-pane="upload" class="profile-avatar-choice" hidden>
                <label for="profileAvatarUpload">Upload image, GIF, or WebP</label>
                <input id="profileAvatarUpload" class="profile-static-upload-input" type="file" accept="image/png,image/jpeg,image/gif,image/webp" tabindex="-1" aria-hidden="true" onchange="_handleProfileAvatarUpload(this.files&&this.files[0])">
                <div class="profile-static-upload-row">
                  <button type="button" class="profile-static-upload-trigger" onclick="$('profileAvatarUpload')&&$('profileAvatarUpload').click()">Choose image</button>
                  <span class="profile-static-upload-status">PNG, JPEG, GIF, or WebP up to 3 MB.</span>
                </div>
              </div>
              <div data-profile-avatar-pane="url" class="profile-avatar-choice" hidden>
                <label for="profileAvatarUrl">Image or GIF URL</label>
                <input id="profileAvatarUrl" type="url" placeholder="https://example.com/avatar.gif" oninput="_refreshProfileAvatarDialogPreview()">
              </div>
              <div data-profile-avatar-pane="asset" class="profile-avatar-choice" hidden>
                <label for="profileAvatarAsset">Safe asset reference</label>
                <input id="profileAvatarAsset" type="text" spellcheck="false" placeholder="avatars/researcher.webp" oninput="_refreshProfileAvatarDialogPreview()">
              </div>
            </section>
            <section id="profileAvatarReactiveEditor" class="profile-avatar-reactive-editor profile-avatar-editor-section" aria-label="Reactive avatar animation slots" hidden>
              <div class="profile-avatar-section-label">Animation slots</div>
              ${slotRows}
            </section>
          </div>
        </div>
        <div id="profileAvatarDialogMessage" class="profile-avatar-dialog-message" hidden></div>
        <div class="profile-avatar-dialog-actions">
          <button type="button" class="ws-action-btn ghost" onclick="_saveProfileAvatar('${profileName}',true)">Clear static</button>
          <button type="button" class="ws-action-btn ghost" onclick="_saveProfileAvatar('${profileName}',false,{clearReactive:true})">Clear animations</button>
          <div style="flex:1"></div>
          <button type="button" class="ws-action-btn ghost" onclick="_closeProfileAvatarDialog()">Cancel</button>
          <button id="profileAvatarSave" type="button" class="ws-action-btn" onclick="_saveProfileAvatar('${profileName}')">Save avatar</button>
        </div>
      </div>
    </div>`;
}


function _bindProfileOpsConsole(p, isActive, isDefault){
  const profileName = p.name;
  // Avatar — both the hero avatar tile and the corner pencil open the dialog.
  const heroAvatar = $('profileHeroAvatar');
  const heroAvatarEdit = $('profileHeroAvatarEdit');
  const openAvatar = () => _openProfileAvatarDialog(profileName);
  if (heroAvatar) {
    heroAvatar.onclick = openAvatar;
    heroAvatar.onkeydown = (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openAvatar(); }
    };
  }
  if (heroAvatarEdit) heroAvatarEdit.onclick = (ev) => { ev.stopPropagation(); openAvatar(); };

  // Description — click the line or the pencil to enter inline-edit mode.
  // The actual save happens in _exitProfileDescriptionEdit (POST settings).
  const heroDesc = $('profileHeroDescription');
  const heroDescEdit = $('profileHeroDescriptionEdit');
  const enterDescEdit = () => _enterProfileDescriptionEdit(p);
  if (heroDesc) {
    heroDesc.addEventListener('click', (ev) => {
      // Ignore clicks inside the active editor (textarea, action buttons).
      if (heroDesc.dataset.editing === '1') return;
      ev.preventDefault();
      enterDescEdit();
    });
    heroDesc.addEventListener('keydown', (ev) => {
      if (heroDesc.dataset.editing === '1') return;
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); enterDescEdit(); }
    });
  }
  if (heroDescEdit) heroDescEdit.onclick = (ev) => { ev.stopPropagation(); enterDescEdit(); };

  // Make active (only present when profile is inactive)
  const makeActive = $('opsMakeActive');
  if (makeActive && !isActive) {
    makeActive.addEventListener('click', async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (makeActive.disabled) return;
      makeActive.disabled = true;
      makeActive.setAttribute('aria-disabled', 'true');
      makeActive.setAttribute('aria-busy', 'true');
      try {
        await _activateProfileFromPanel(profileName);
      } catch (e) {
        makeActive.disabled = false;
        makeActive.removeAttribute('aria-disabled');
        makeActive.removeAttribute('aria-busy');
        showToast('Failed to activate: ' + (e.message || e));
      }
    });
  }

  // Hero overflow menu (top-right "⋯") — owns Rename / Change description /
  // Duplicate / Remove. The action buttons inline beneath the description
  // are reserved for Make Active.
  const body = $('profileDetailBody');
  const heroMenuBtn = $('profileHeroMenuButton');
  const heroMenu = $('profileHeroMenu');
  const closeHeroMenu = () => {
    if (!heroMenu || heroMenu.hidden) return;
    heroMenu.hidden = true;
    if (heroMenuBtn) heroMenuBtn.setAttribute('aria-expanded', 'false');
    document.removeEventListener('click', heroMenuOutside, true);
    document.removeEventListener('keydown', heroMenuEsc, true);
  };
  const heroMenuOutside = (ev) => {
    if (!heroMenu || heroMenu.contains(ev.target) || (heroMenuBtn && heroMenuBtn.contains(ev.target))) return;
    closeHeroMenu();
  };
  const heroMenuEsc = (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); closeHeroMenu(); if (heroMenuBtn) heroMenuBtn.focus(); } };
  if (heroMenuBtn && heroMenu) {
    heroMenuBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      if (heroMenu.hidden) {
        heroMenu.hidden = false;
        heroMenuBtn.setAttribute('aria-expanded', 'true');
        // Defer registration so the opening click itself doesn't immediately close it.
        setTimeout(() => {
          document.addEventListener('click', heroMenuOutside, true);
          document.addEventListener('keydown', heroMenuEsc, true);
        }, 0);
        const first = heroMenu.querySelector('.profile-hero-menu-item:not([disabled])');
        if (first) first.focus();
      } else {
        closeHeroMenu();
      }
    });
  }

  if (body) {
    body.querySelectorAll('[data-ops-action="rename"]').forEach(btn => {
      btn.onclick = () => { closeHeroMenu(); _opsRenameProfile(profileName); };
    });
    body.querySelectorAll('[data-ops-action="duplicate"]').forEach(btn => {
      btn.onclick = () => { closeHeroMenu(); _opsDuplicateProfile(profileName); };
    });
    body.querySelectorAll('[data-ops-action="edit-description"]').forEach(btn => {
      btn.onclick = () => { closeHeroMenu(); _enterProfileDescriptionEdit(p); };
    });
    body.querySelectorAll('[data-ops-action="remove"]').forEach(btn => {
      if (isDefault) return;  // default profile menu item stays disabled
      btn.onclick = () => { closeHeroMenu(); deleteCurrentProfile(); };
    });
    body.querySelectorAll('[data-ops-action="skills"]').forEach(btn => {
      btn.onclick = () => _openProfileSkillsManager(profileName);
    });
    // Platforms button on the Gateway tile (spec 2026-05-16).
    body.querySelectorAll('[data-platforms-action]').forEach(btn => {
      btn.onclick = (ev) => { ev.stopPropagation(); _openPlatformsManager(profileName); };
    });
  }

  // Gateway toggle (v3 — replaces data-gateway-action button row).
  if (body) {
    const toggle = body.querySelector('[data-gateway-toggle]');
    if (toggle) {
      toggle.onclick = () => _onGatewayToggle(profileName);
    }
    const info = body.querySelector('[data-gateway-info]');
    if (info) {
      info.onclick = () => _openGatewayInfoDialog(profileName);
    }
  }
  // Kick off an initial status read for this profile so the seed phase
  // is replaced by the authoritative server-side phase, then keep polling
  // while this detail view remains visible (slow for stable phases, fast
  // for starting/stopping).
  _refreshGatewayStatus(profileName).then(() => {
    _startGatewayPoller(profileName);
  }).catch(() => {});
  // Warm the platforms cache for the tile's count badge. The modal will
  // reuse this payload on first open if the 30s TTL hasn't elapsed.
  _loadProfilePlatforms(profileName, { force: false })
    .then(() => _repaintGatewayTile(profileName))
    .catch(() => {});

  // Profile file widgets — scoped to the detail body (review F2).
  if (body) body.querySelectorAll('[data-profile-file]').forEach(btn => {
    btn.onclick = () => _openProfileFileEditor(profileName, btn.dataset.profileFile);
  });
}

// v2 overflow-menu helpers — kept temporarily so any lingering reference
// doesn't ReferenceError; the v3 hero doesn't use them. Safe to remove in
// the cleanup commit (Task 18).
function _opsOverflowOutsideClick(ev){ /* v2 only — no-op in v3 */ }
function _opsOverflowEscape(ev){ /* v2 only — no-op in v3 */ }

async function _profileChatDefaults(profileName){
  const summary = (_profilesCache && Array.isArray(_profilesCache.profiles))
    ? _profilesCache.profiles.find(p => p && p.name === profileName)
    : null;
  let settings = null;
  if (_currentProfileDetail && _currentProfileDetail.name === profileName && _profileRuntimeSettings) {
    settings = Object.assign({}, _currentProfileDetail, _profileRuntimeSettings);
  } else {
    try {
      settings = await api('/api/profile/settings?name=' + encodeURIComponent(profileName) + '&include_avatar=0');
    } catch (_) {
      settings = {};
    }
  }
  settings = settings || {};
  const hasWorkspace = Object.prototype.hasOwnProperty.call(settings, 'default_workspace');
  return {
    profile: profileName,
    model: settings.model || (summary && summary.model) || '',
    model_provider: settings.provider || (summary && summary.provider) || null,
    workspace: hasWorkspace ? (settings.default_workspace || null) : null,
  };
}

async function startChatWithProfile(profileName){
  if (!profileName) return;
  try {
    const defaults = await _profileChatDefaults(profileName);
    await newSession(true, defaults);
    if (typeof switchPanel === 'function') switchPanel('chat');
    if (typeof closeMobileSidebar === 'function') closeMobileSidebar();
    const msg = $('msg');
    if (msg) msg.focus();
  } catch (e) {
    showToast('Start chat failed: ' + (e.message || e));
  }
}

// ── In-app input dialog (replaces window.prompt) ────────────────────────
//
// showInputDialog renders a styled modal and returns a Promise<string|null>
// that resolves to the entered string on Save, or null on Cancel / Esc /
// click-outside. It enforces an optional `maxlength` + `pattern` and
// surfaces a validation message inline so the user never leaves the dialog
// for a bad input.

const _PROFILE_NAME_MAX_LEN = 32;
const _PROFILE_NAME_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;
const _PROFILE_NAME_HINT = 'Lowercase letters, digits, dash and underscore. Must start with a letter or digit.';

function showInputDialog({
  title, message, defaultValue, confirmLabel,
  maxlength, pattern, patternError, placeholder, hint,
} = {}) {
  return new Promise((resolve) => {
    const existing = document.getElementById('inputDialogOverlay');
    if (existing) existing.remove();
    const overlay = document.createElement('div');
    overlay.id = 'inputDialogOverlay';
    overlay.className = 'input-dialog';
    overlay.innerHTML = `
      <div class="input-dialog-card" role="dialog" aria-modal="true" aria-labelledby="inputDialogTitle">
        <div class="input-dialog-head">
          <div>
            <div id="inputDialogTitle" class="input-dialog-title">${esc(title || 'Enter value')}</div>
            ${message ? `<div class="input-dialog-message">${esc(message)}</div>` : ''}
          </div>
          <button type="button" class="input-dialog-close" data-input-action="cancel" aria-label="Close">×</button>
        </div>
        <input id="inputDialogValue" type="text" autocomplete="off" spellcheck="false"
          ${maxlength ? `maxlength="${maxlength}"` : ''}
          ${placeholder ? `placeholder="${esc(placeholder)}"` : ''}
          value="${esc(defaultValue || '')}">
        <div id="inputDialogCounter" class="input-dialog-counter muted">${maxlength ? `${(defaultValue||'').length}/${maxlength}` : '&nbsp;'}</div>
        <div id="inputDialogError" class="input-dialog-error" hidden></div>
        ${hint ? `<div class="input-dialog-hint muted">${esc(hint)}</div>` : ''}
        <div class="input-dialog-actions">
          <button type="button" class="profile-ops-button" data-input-action="cancel">Cancel</button>
          <button type="button" class="profile-ops-button primary" data-input-action="confirm">${esc(confirmLabel || 'OK')}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const input = overlay.querySelector('#inputDialogValue');
    const counter = overlay.querySelector('#inputDialogCounter');
    const errorEl = overlay.querySelector('#inputDialogError');

    const cleanup = (value) => {
      document.removeEventListener('keydown', onKey, true);
      overlay.remove();
      resolve(value);
    };
    const cancel = () => cleanup(null);
    const confirmInput = () => {
      const v = (input.value || '').trim();
      if (!v) { showError('A value is required.'); input.focus(); return; }
      if (maxlength && v.length > maxlength) { showError(`Must be ${maxlength} characters or fewer.`); input.focus(); return; }
      if (pattern && !pattern.test(v)) { showError(patternError || 'Invalid value.'); input.focus(); return; }
      cleanup(v);
    };
    const showError = (msg) => {
      if (!errorEl) return;
      errorEl.textContent = msg;
      errorEl.hidden = false;
    };

    overlay.addEventListener('click', (ev) => { if (ev.target === overlay) cancel(); });
    overlay.querySelectorAll('[data-input-action]').forEach(btn => {
      btn.addEventListener('click', () => {
        if (btn.dataset.inputAction === 'confirm') confirmInput(); else cancel();
      });
    });
    input.addEventListener('input', () => {
      if (counter && maxlength) counter.textContent = `${input.value.length}/${maxlength}`;
      if (errorEl && !errorEl.hidden) errorEl.hidden = true;
    });
    const onKey = (ev) => {
      if (ev.key === 'Escape') { ev.preventDefault(); cancel(); }
      else if (ev.key === 'Enter') { ev.preventDefault(); confirmInput(); }
    };
    document.addEventListener('keydown', onKey, true);

    // Defer focus so the input doesn't capture the click that opened it.
    setTimeout(() => {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }, 0);
  });
}

async function _opsRenameProfile(profileName){
  if (!profileName) return;
  const newName = await showInputDialog({
    title: 'Rename profile',
    message: `Choose a new name for "${profileName}".`,
    defaultValue: profileName,
    confirmLabel: 'Rename',
    maxlength: _PROFILE_NAME_MAX_LEN,
    pattern: _PROFILE_NAME_PATTERN,
    patternError: _PROFILE_NAME_HINT,
    hint: _PROFILE_NAME_HINT,
  });
  if (!newName || newName === profileName) return;
  try {
    const result = await api('/api/profile/rename', { method: 'POST', body: JSON.stringify({ name: profileName, new_name: newName }) });
    if (result && result.was_active) {
      S.activeProfile = result.new_name;
    }
    if (_currentProfileDetail && _currentProfileDetail.name === profileName) {
      _currentProfileDetail = { ..._currentProfileDetail, name: result.new_name };
    }
    showToast(`Renamed to "${result.new_name || newName}"`);
    await loadProfilesPanel();
    if (result && result.new_name) openProfileDetail(result.new_name);
  } catch (e) {
    showToast('Rename failed: ' + (e.message || e));
  }
}

async function _opsDuplicateProfile(profileName){
  if (!profileName) return;
  // Suggested copy name, truncated so the suffix doesn't push us over the cap.
  let suggested = `${profileName}-copy`;
  if (suggested.length > _PROFILE_NAME_MAX_LEN) {
    suggested = suggested.slice(0, _PROFILE_NAME_MAX_LEN);
  }
  const newName = await showInputDialog({
    title: 'Duplicate profile',
    message: `Create a copy of "${profileName}".`,
    defaultValue: suggested,
    confirmLabel: 'Duplicate',
    maxlength: _PROFILE_NAME_MAX_LEN,
    pattern: _PROFILE_NAME_PATTERN,
    patternError: _PROFILE_NAME_HINT,
    hint: _PROFILE_NAME_HINT,
  });
  if (!newName) return;
  try {
    const result = await api('/api/profile/duplicate', {
      method: 'POST',
      body: JSON.stringify({ name: profileName, new_name: newName })
    });
    showToast(`Duplicated to "${newName}"`);
    await loadProfilesPanel();
    const target = result && result.profile && result.profile.name ? result.profile.name : newName;
    openProfileDetail(target);
  } catch (e) {
    showToast('Duplicate failed: ' + (e.message || e));
  }
}

function _openGatewayInfoDialog(profileName){
  const state = _gatewayStateByProfile.get(profileName) || { phase: 'unknown' };
  const existing = document.getElementById('gatewayInfoDialogOverlay');
  if (existing) existing.remove();
  const overlay = document.createElement('div');
  overlay.id = 'gatewayInfoDialogOverlay';
  overlay.className = 'gateway-info-dialog';
  const phase = state.phase || 'unknown';
  const source = state.status_source || '—';
  const reason = _gatewayInfoReason(state);
  const detail = state.detail || state.last_error || 'No additional detail was provided.';
  const copyText = [
    `Profile: ${profileName}`,
    `Phase: ${phase}`,
    `Status source: ${source}`,
    `Health reason: ${reason}`,
    '',
    detail,
  ].join('\n');
  overlay.innerHTML = `
    <div class="gateway-info-card" role="dialog" aria-modal="true" aria-labelledby="gatewayInfoDialogTitle">
      <div class="input-dialog-head">
        <div>
          <div id="gatewayInfoDialogTitle" class="input-dialog-title">Gateway status details</div>
          <div class="input-dialog-message">Copyable runtime detail for the selected profile gateway.</div>
        </div>
        <button type="button" class="input-dialog-close" data-gateway-info-action="close" aria-label="Close">×</button>
      </div>
      <dl class="gateway-info-grid">
        <dt>Profile</dt><dd>${esc(profileName)}</dd>
        <dt>Phase</dt><dd>${esc(_gatewayLabelForPhase(phase))}</dd>
        <dt>Status source</dt><dd>${esc(source)}</dd>
        <dt>Health reason</dt><dd>${esc(reason)}</dd>
      </dl>
      <textarea class="gateway-info-detail" readonly rows="8">${esc(detail)}</textarea>
      <div class="input-dialog-actions">
        <button type="button" class="profile-ops-button" data-gateway-info-action="copy">Copy</button>
        <button type="button" class="profile-ops-button primary" data-gateway-info-action="close">Close</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  const detailEl = overlay.querySelector('.gateway-info-detail');
  const closeBtn = overlay.querySelector('[data-gateway-info-action="close"]');
  const opener = document.querySelector(`.profile-gateway-tile[data-profile-name="${CSS.escape(profileName)}"] [data-gateway-info]`);
  const cleanup = () => {
    document.removeEventListener('keydown', onKey, true);
    overlay.remove();
    if (opener) opener.focus();
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(copyText);
      showToast('Gateway details copied');
    } catch (_) {
      if (detailEl) {
        detailEl.focus();
        detailEl.select();
        try { document.execCommand('copy'); showToast('Gateway details copied'); }
        catch (e) { showToast('Copy failed: ' + (e && e.message || e)); }
      }
    }
  };
  const onKey = (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); cleanup(); } };
  overlay.addEventListener('click', (ev) => { if (ev.target === overlay) cleanup(); });
  overlay.querySelectorAll('[data-gateway-info-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.dataset.gatewayInfoAction === 'copy') copy(); else cleanup();
    });
  });
  document.addEventListener('keydown', onKey, true);
  setTimeout(() => { if (closeBtn) closeBtn.focus(); }, 0);
}

// ── Gateway toggle handler + poller (v3) ──────────────────────────────

async function _onGatewayToggle(profileName){
  if (!profileName) return;
  const state = _gatewayStateByProfile.get(profileName) || { phase: 'stopped' };
  const phase = state.phase || 'stopped';
  if (phase === 'starting' || phase === 'stopping') return;  // locked
  if (state.control_available === false || phase === 'unavailable') {
    _openGatewayInfoDialog(profileName);
    return;
  }

  // Cancel any in-flight poller before the action so a stale 12s-cadence
  // poll started from the running-state initial render cannot fire during
  // the POST window and momentarily flip the UI back to 'stopping' after
  // the action has already settled to 'stopped'. The post-action setTimeout
  // below kicks off a fresh poller with the correct cadence.
  _stopGatewayPoller(profileName);

  const action = (phase === 'running') ? 'stop' : 'start';
  const optimisticPhase = (action === 'start') ? 'starting' : 'stopping';
  _gatewayStateByProfile.set(profileName, {
    ...state, phase: optimisticPhase, last_error: null, detail: null,
  });
  _repaintGatewayTile(profileName);

  try {
    const result = await api('/api/profile/gateway', {
      method: 'POST',
      body: JSON.stringify({ name: profileName, action }),
    });
    if (result && result.ok === false) {
      _gatewayStateByProfile.set(profileName, {
        ...state,
        phase: result.phase || 'failed',
        last_error: result.message || result.last_error || 'Gateway action failed.',
        detail: result.detail || result.message || 'Gateway action failed.',
        status_source: result.status_source || state.status_source || null,
        health: result.health || state.health || null,
        control_available: result.control_available !== false,
        phase_started_at: null,
        pid: null,
      });
      _repaintGatewayTile(profileName);
      showToast(result.message || 'Gateway action failed.');
      return;
    }
    if (result && result.phase) {
      _gatewayStateByProfile.set(profileName, _normalizeGatewayStatus(result, state));
      _repaintGatewayTile(profileName);
    }
    // Fast-track first refresh, then let the visible poller choose cadence.
    setTimeout(() => _refreshGatewayStatus(profileName).then(() => {
      _startGatewayPoller(profileName);
    }).catch(() => {}), 250);
  } catch (e) {
    _gatewayStateByProfile.set(profileName, {
      ...state,
      phase: 'failed',
      last_error: String((e && e.message) || e),
      detail: String((e && e.message) || e),
      phase_started_at: null,
      pid: null,
    });
    _repaintGatewayTile(profileName);
    showToast('Gateway action failed: ' + (e && e.message || e));
  }
}

function _normalizeGatewayStatus(result, prior){
  const prev = prior || {};
  return {
    ...prev,
    phase: result.phase || 'stopped',
    desired_enabled: result.desired_enabled === true,
    control_available: result.control_available !== false,
    status_source: result.status_source || null,
    health: result.health || null,
    detail: result.detail || null,
    last_error: result.last_error || result.detail || null,
    phase_started_at: result.phase_started_at || null,
    pid: result.pid || null,
    updated_at: result.updated_at || null,
  };
}

async function _refreshGatewayStatus(profileName){
  try {
    const result = await api(
      '/api/profile/gateway/status?name=' + encodeURIComponent(profileName)
    );
    if (!result || result.ok !== true) return null;
    // Cache canonical contract fields: control_available, status_source,
    // health, detail, desired_enabled, plus compatibility phase/pid fields.
    _gatewayStateByProfile.set(profileName, _normalizeGatewayStatus(result, _gatewayStateByProfile.get(profileName)));
    _repaintGatewayTile(profileName);
    return result;
  } catch (_) {
    // Silent — leave previous state, the next poll will retry.
    return null;
  }
}

function _gatewayDetailVisible(profileName){
  if (document.visibilityState === 'hidden') return false;
  if (_currentPanel !== 'profiles') return false;
  if (!_currentProfileDetail || _currentProfileDetail.name !== profileName) return false;
  return !!document.querySelector(`.profile-gateway-tile[data-profile-name="${CSS.escape(profileName)}"]`);
}

function _startGatewayPoller(profileName){
  if (_gatewayPollers.has(profileName)) return;  // already polling
  const schedule = (delay) => {
    const handle = setTimeout(async () => {
      if (document.visibilityState === 'hidden' || _currentPanel !== 'profiles' || !_currentProfileDetail || _currentProfileDetail.name !== profileName) { _stopGatewayPoller(profileName); return; }
      if (!_gatewayDetailVisible(profileName)) { _stopGatewayPoller(profileName); return; }
      const result = await _refreshGatewayStatus(profileName);
      if (!_gatewayDetailVisible(profileName)) { _stopGatewayPoller(profileName); return; }
      const state = _gatewayStateByProfile.get(profileName) || {};
      const phase = (result && result.phase) || state.phase || 'stopped';
      schedule(_GATEWAY_TRANSIENT_PHASES.has(phase) ? _GATEWAY_TRANSIENT_POLL_MS : _GATEWAY_STABLE_POLL_MS);
    }, delay);
    _gatewayPollers.set(profileName, handle);
  };
  const state = _gatewayStateByProfile.get(profileName) || {};
  schedule(_GATEWAY_TRANSIENT_PHASES.has(state.phase) ? _GATEWAY_TRANSIENT_POLL_MS : _GATEWAY_STABLE_POLL_MS);
}

function _stopGatewayPoller(profileName){
  const handle = _gatewayPollers.get(profileName);
  if (handle) {
    clearTimeout(handle);
    _gatewayPollers.delete(profileName);
  }
}

function _stopAllGatewayPollers(){
  for (const handle of _gatewayPollers.values()) clearTimeout(handle);
  _gatewayPollers.clear();
}

async function _openProfileFileEditor(profileName, filename) {
  const body = $('profileDetailBody');
  const title = $('profileDetailTitle');
  if (!body || !title) return;
  const _editorLabels = {'SOUL.md': 'SOUL.md', 'config.yaml': 'config.yaml', '.env': '.env', 'memories/MEMORY.md': 'Agent Memory', 'memories/USER.md': 'User Profile'};
  const friendlyName = _editorLabels[filename] || filename;
  title.innerHTML = `<span style="cursor:pointer;color:var(--link)" onclick="openProfileDetail('${esc(profileName)}')">${esc(profileName)}</span> / ${esc(friendlyName)}`;
  body.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:12px">Loading...</div>';
  body.style.display = '';
  try {
    const qs = new URLSearchParams({name: profileName, file: filename});
    const data = await api('/api/profile/files?' + qs);
    const fileContent = (data && data.content != null) ? data.content : '';
    const placeholder = filename === 'SOUL.md'
      ? 'Define the agent personality, mission, and behavioral rules...'
      : filename === 'config.yaml'
      ? 'YAML configuration for model, provider, agent settings...'
      : filename === 'memories/MEMORY.md'
      ? 'Agent persistent memory - facts, patterns, and learned context...'
      : filename === 'memories/USER.md'
      ? 'User profile - preferences, communication style, personal context...'
      : 'Environment variables (KEY=value per line)...';
    body.innerHTML = `
      <div class="main-view-content">
        <div class="detail-card">
          <div style="display:flex;justify-content:flex-end;gap:8px;margin-bottom:8px">
            <button class="panel-head-btn profile-file-editor-action has-tooltip has-tooltip--bottom" data-tooltip="Back" onclick="openProfileDetail('${esc(profileName)}')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 18 9 12 15 6"/></svg><span class="panel-head-btn-label">Back</span></button>
            <button id="btnSaveProfileFile" class="panel-head-btn primary profile-file-editor-action has-tooltip has-tooltip--bottom" data-tooltip="Save" onclick="_saveProfileFile('${esc(profileName)}','${esc(filename)}')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg><span class="panel-head-btn-label">Save</span></button>
          </div>
          <textarea id="profileFileEditor" rows="24" spellcheck="false"
            style="width:100%;resize:vertical;font-family:var(--font-mono,monospace);font-size:13px;line-height:1.5;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg)"
            placeholder="${esc(placeholder)}">${esc(fileContent)}</textarea>
        </div>
      </div>`;
  } catch (e) {
    body.innerHTML = `<div style="padding:16px;color:var(--accent);font-size:12px">Error loading file: ${esc(e.message)}</div>`;
  }
}

async function _saveProfileFile(profileName, filename) {
  const editor = $('profileFileEditor');
  const btn = $('btnSaveProfileFile');
  if (!editor) return;
  const content = editor.value;
  if (btn) { btn.disabled = true; btn.style.opacity = '0.5'; }
  try {
    const result = await api('/api/profile/files', {
      method: 'POST',
      body: JSON.stringify({name: profileName, file: filename, content: content})
    });
    if (result && result.ok) {
      showToast(filename + ' saved');
    } else {
      showToast('Save failed: ' + (result.error || 'unknown error'));
    }
  } catch (e) {
    showToast('Save failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.style.opacity = ''; }
  }
}

function _setProfileHeaderButtons(mode, p, activeName){
  const cancelBtn = $('btnCancelProfileDetail');
  const saveBtn = $('btnSaveProfileDetail');
  const show = b => b && (b.style.display = '');
  const hide = b => b && (b.style.display = 'none');
  if (mode === 'read') {
    hide(cancelBtn); hide(saveBtn);
  } else if (mode === 'create') {
    show(cancelBtn); show(saveBtn);
  } else {
    [cancelBtn, saveBtn].forEach(hide);
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
  _stopAllGatewayPollers();
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
  await _activateProfileFromPanel(_currentProfileDetail.name);
}

async function _activateProfileFromPanel(profileName){
  if (!profileName) return null;
  const data = await api('/api/profile/switch', {
    method: 'POST',
    body: JSON.stringify({ name: profileName }),
  });
  S.activeProfile = (data && data.active) || profileName;
  if (data) _syncProfileAvatarState(data);
  const chipLabel = $('profileChipLabel');
  if (chipLabel) chipLabel.textContent = S.activeProfile;
  if (typeof applyBotName === 'function') applyBotName();
  if (typeof syncTopbar === 'function') syncTopbar();
  await loadProfilesPanel();
  if (typeof renderSessionList === 'function') {
    Promise.resolve(renderSessionList()).catch(() => {});
  }
  showToast(t('profile_switched', S.activeProfile));
  return data;
}

async function deleteCurrentProfile(){
  if (!_currentProfileDetail) return;
  const name = _currentProfileDetail.name;
  const _ok = await showConfirmDialog({title:t('profile_delete_confirm_title',name),message:t('profile_delete_confirm_message'),confirmLabel:t('delete_title'),danger:true,focusCancel:true});
  if(!_ok) return;
  try {
    await api('/api/profile/delete', { method: 'POST', body: JSON.stringify({ name }) });
    _invalidateKanbanProfileCache();
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
  const selectedProfile = (typeof currentSessionProfile === 'function') ? currentSessionProfile() : S.activeProfile;
  const active = (selectedProfile && profiles.some(p => p.name === selectedProfile))
    ? selectedProfile
    : (data.active || 'default');
  _syncProfileAvatarState(data);
  for (const p of profiles) {
    const opt = document.createElement('div');
    opt.className = 'profile-opt' + (p.name === active ? ' active' : '');
    opt.dataset.name = p.name;
    const meta = [];
    if (p.model) meta.push(p.model.split('/').pop());
    const skillsMeta = _profileSkillsCountMeta(p);
    if (skillsMeta) meta.push(skillsMeta);
    const gwDot = `<span class="profile-opt-badge ${p.gateway_running ? 'running' : 'stopped'}"></span>`;
    const checkmark = p.name === active ? ' <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--link)" stroke-width="3" style="vertical-align:-1px"><polyline points="20 6 9 17 4 12"/></svg>' : '';
    const defaultBadge = p.is_default ? ` <span style="opacity:.5;font-weight:400">${esc(t('profile_default_label'))}</span>` : '';
    opt.innerHTML = `<div class="profile-opt-main">${_profileAvatarForUi(p,'profile-avatar--dropdown')}<div style="min-width:0;flex:1"><div class="profile-opt-name">${gwDot}${esc(p.name)}${defaultBadge}${checkmark}</div>` +
      (meta.length ? `<div class="profile-opt-meta">${esc(meta.join(' \u00b7 '))}</div>` : '') + `</div></div>`;
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
  // Profile switches are per-client cookie/TLS scoped, so a running stream in
  // the current session can safely continue while this tab moves to another
  // profile. The in-flight session stays attached to its original profile.

  // ── Loading indicator ───────────────────────────────────────────────────
  // Show spinner on the profile chip immediately so the user gets visual
  // feedback while the async switch is in progress.
  const _chip = $('profileChip');
  const _chipLabel = $('profileChipLabel');
  const _prevProfileName = S.activeProfile || 'default';
  const _switchGen = ++_profileSwitchGeneration;
  if (_chip) { _chip.classList.add('switching'); _chip.disabled = true; }
  // Optimistic name update — shows the target name right away
  if (_chipLabel) _chipLabel.textContent = name;

  const visibleSessionProfile = S.session
    ? (typeof currentSessionProfile === 'function' ? currentSessionProfile() : (S.session.profile || S.activeProfile || 'default'))
    : (S.activeProfile || 'default');
  const sessionHasLiveWork = Boolean(S.session && (
    S.session.active_stream_id ||
    S.session.pending_user_message
  ));
  // Determine whether the current session has any messages.
  // A session with messages is "in progress" and belongs to the current profile —
  // we must not retag it.  We'll start a fresh session for the new profile instead.
  const sessionInProgress = S.session && (
    (S.messages && S.messages.length > 0) ||
    S.session.active_stream_id ||
    S.session.pending_user_message
  );

  try {
    const data = await api('/api/profile/switch', { method: 'POST', body: JSON.stringify({ name }) });
    if (_switchGen !== _profileSwitchGeneration) return;
    const targetProfile = String((data && data.active) || name || 'default').trim() || 'default';
    const profileDefaultModel = String((data && data.default_model) || '').trim();
    const profileDefaultProvider = String((data && (data.active_provider || data.default_model_provider)) || '').trim() || null;
    const profileDefaultWorkspace = data && data.default_workspace ? data.default_workspace : null;
    const needsFreshProfileSession = Boolean(S.session && (sessionInProgress || visibleSessionProfile!==targetProfile));
    S.activeProfile = targetProfile;
    _syncProfileAvatarState(data);
    if(typeof refreshActiveProfileAvatarSettings==='function') void refreshActiveProfileAvatarSettings(S.activeProfile);

    // Update composer placeholder and title bar while the core profile-switch
    // state is still close to the profile API response.
    if (typeof applyBotName === 'function') applyBotName();

    // ── Model + Workspace ──────────────────────────────────────────────────
    // Apply the profile defaults returned by /api/profile/switch immediately.
    // The full model/workspace catalogs are refreshed in the background after
    // the visible chat has moved to the requested profile.
    if(typeof _clearPersistedModelState==='function') _clearPersistedModelState();
    else localStorage.removeItem('hermes-webui-model');
    _skillsData = null;
    _workspaceList = null;
    if(profileDefaultModel) window._defaultModel = profileDefaultModel;
    if(profileDefaultProvider) window._activeProvider = profileDefaultProvider;

    // ── Apply model ────────────────────────────────────────────────────────
    if (profileDefaultModel) {
      window._defaultModel = profileDefaultModel;
      const sel = $('modelSelect');
      const providerId = profileDefaultProvider || window._activeProvider || null;
      const existingDefaultOpt = sel ? Array.from(sel.options).find(o => o.value === profileDefaultModel) : null;
      if (existingDefaultOpt && providerId && !existingDefaultOpt.dataset.provider) {
        existingDefaultOpt.dataset.provider = providerId;
      }
      if (sel && !existingDefaultOpt) {
        const opt = document.createElement('option');
        opt.value = profileDefaultModel;
        opt.textContent = typeof getModelLabel === 'function' ? getModelLabel(profileDefaultModel) : profileDefaultModel;
        opt.dataset.custom = '1';
        if (providerId) opt.dataset.provider = providerId;
        sel.querySelectorAll('option[data-custom]').forEach(o => o.remove());
        sel.appendChild(opt);
      }
      const resolved = _applyModelToDropdown(profileDefaultModel, sel, providerId);
      const modelToUse = resolved || profileDefaultModel;
      const modelState = (typeof _modelStateForSelect==='function')
        ? _modelStateForSelect(sel, modelToUse)
        : {model:modelToUse, model_provider:providerId};
      S._pendingProfileModel = modelToUse;
      S._pendingProfileModelProvider = profileDefaultProvider || modelState.model_provider || providerId || null;
      // Only patch the in-memory session model if we're NOT about to replace the session
      if (S.session && !needsFreshProfileSession) {
        S.session.model = modelToUse;
        S.session.model_provider = profileDefaultProvider || modelState.model_provider || providerId || null;
      }
    }

    // ── Apply workspace ────────────────────────────────────────────────────
    if (profileDefaultWorkspace) {
      // Always store the persistent profile default — used for blank-page display
      // and workspace auto-bind throughout the session lifecycle (#804, #823).
      S._profileDefaultWorkspace = profileDefaultWorkspace;
      // Also set the one-shot flag consumed by newSession() so the first new
      // session after a profile switch inherits this workspace (#424).
      S._profileSwitchWorkspace = profileDefaultWorkspace;

      if (S.session && !needsFreshProfileSession) {
        // Empty session (no messages yet) — safe to update it in place
        const sessionId = S.session.session_id;
        S.session.workspace = profileDefaultWorkspace;
        try {
          void api('/api/session/update', { method: 'POST', body: JSON.stringify({
            session_id: sessionId,
            workspace: profileDefaultWorkspace,
            model: S.session.model,
            model_provider: S.session.model_provider||null,
          })}).catch(() => {});
        } catch (_) {}
      }
    }

    // ── Session ────────────────────────────────────────────────────────────
    _showAllProfiles = false;
    if (typeof animateNextSessionListRefresh === 'function') animateNextSessionListRefresh();

    if (sessionInProgress) {
      // The current session has messages and belongs to the previous profile.
      // Start a new session for the new profile so nothing gets cross-tagged.
      const workspaceVisible = typeof _workspacePanelMode !== 'undefined' && _workspacePanelMode !== 'closed';
      await newSession(false, {
        profile:targetProfile,
        ...(profileDefaultModel?{model:profileDefaultModel,model_provider:profileDefaultProvider||null}:{}),
        ...(sessionHasLiveWork?{commitPrevious:false}:{}),
        awaitWorkspaceLoad: workspaceVisible,
      });
      if (_switchGen !== _profileSwitchGeneration) return;
      // Keep topbar chips (workspace/profile) in sync after creating the
      // new profile-scoped session.
      syncTopbar();
      if(typeof renderSessionList==='function') void renderSessionList({deferWhileInteracting:true});
      showToast(t('profile_switched_new_conversation', targetProfile));
    } else if (needsFreshProfileSession) {
      // The visible session belongs to another profile even if it has not yet
      // accumulated visible messages. Create a correctly owned target-profile
      // chat so syncTopbar() does not snap the picker back to the old owner.
      await newSession(false, {
        profile:targetProfile,
        ...(profileDefaultModel?{model:profileDefaultModel,model_provider:profileDefaultProvider||null}:{}),
        ...(sessionHasLiveWork?{commitPrevious:false}:{}),
      });
      syncTopbar();
      if(typeof renderSessionList==='function') void renderSessionList({deferWhileInteracting:true});
      showToast(t('profile_switched_new_conversation', targetProfile));
    } else {
      // No messages yet — just refresh the list and topbar in place
      await renderSessionList();
      if (_switchGen !== _profileSwitchGeneration) return;
      syncTopbar();
      // Refresh workspace file tree so the right panel shows the new
      // profile's workspace, not the previous one (#1214).
      if (S.session && S.session.workspace) {
        const dirLoad = loadDir('.');
        if (typeof _workspacePanelMode !== 'undefined' && _workspacePanelMode !== 'closed') await dirLoad;
      }
      showToast(t('profile_switched', targetProfile));
    }

    await _profileSwitchPanelLoad();
    _refreshProfileSwitchBackground(_switchGen);

  } catch (e) {
    // Revert the optimistic name update on error
    if (_switchGen === _profileSwitchGeneration && _chipLabel) _chipLabel.textContent = _prevProfileName;
    if (_switchGen === _profileSwitchGeneration) showToast(t('switch_failed') + e.message);
  } finally {
    // Always remove loading indicator regardless of success or failure
    if (_switchGen === _profileSwitchGeneration && _chip) { _chip.classList.remove('switching'); _chip.disabled = false; }
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
          <label for="profileFormModel">${esc(t('profile_model_label') || 'Model / provider')}</label>
          <select id="profileFormModel"></select>
          <div class="detail-form-hint">${esc(t('profile_model_hint') || 'Choose from configured providers and models for this new profile.')}</div>
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
  _populateProfileFormModelSelect();
}

async function _populateProfileFormModelSelect(){
  const sel = $('profileFormModel');
  if (!sel) return;
  sel.innerHTML = `<option value="">${esc(t('profile_model_use_default') || 'Use active profile default')}</option>`;
  try {
    const data = await api('/api/models');
    const groups = (Array.isArray(data && data.groups) && data.groups.length) ? data.groups : [];
    for (const g of groups) {
      const og = document.createElement('optgroup');
      og.label = g.provider || g.provider_id || 'Configured';
      if (g.provider_id) og.dataset.provider = g.provider_id;
      for (const m of (Array.isArray(g.models) ? g.models : [])) {
        if (!m || !m.id) continue;
        const opt = document.createElement('option');
        opt.value = m.id;
        opt.textContent = m.label || m.id;
        og.appendChild(opt);
      }
      if (og.children.length) sel.appendChild(og);
    }
    if (data && data.default_model && typeof _applyModelToDropdown === 'function') {
      _applyModelToDropdown(data.default_model, sel, data.active_provider || window._activeProvider || null);
    }
  } catch (e) {
    console.warn('Failed to load profile model picker:', e.message);
  }
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
  const modelEl = $('profileFormModel');
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
    const selectedModel = modelEl ? (modelEl.value || '').trim() : '';
    if (selectedModel) {
      const modelState = (typeof _modelStateForSelect === 'function')
        ? _modelStateForSelect(modelEl, selectedModel)
        : { model: selectedModel, model_provider: null };
      if (modelState.model) payload.default_model = modelState.model;
      if (modelState.model_provider) payload.model_provider = modelState.model_provider;
    }
    if (baseUrl) payload.base_url = baseUrl;
    if (apiKey) payload.api_key = apiKey;
    await api('/api/profile/create', { method: 'POST', body: JSON.stringify(payload) });
    _invalidateKanbanProfileCache();
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
    _invalidateKanbanProfileCache();
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
let _settingsAvatarPresenceLayoutOnOpen = null; // track avatar placement at open time for saved-state bookkeeping
let _settingsHermesDefaultModelOnOpen = '';
let _settingsSection = 'conversation';
let _currentSettingsSection = 'conversation';
let _settingsAppearanceAutosaveTimer = null;
let _settingsAppearanceAutosaveRetryPayload = null;
let _settingsPreferencesAutosaveTimer = null;
let _settingsPreferencesAutosaveRetryPayload = null;

// ── Sidebar tab visibility ─────────────────────────────────────────────────
const _ALWAYS_VISIBLE_TABS = new Set(['chat','settings']);
const _HIDDEN_TABS_LS_KEY = 'hermes-webui-hidden-tabs';

function _getHiddenTabs(){
  try{var h=localStorage.getItem(_HIDDEN_TABS_LS_KEY);if(h){var p=JSON.parse(h);if(Array.isArray(p))return p;}}catch(e){}
  return[];
}

function _setHiddenTabs(panels){
  try{localStorage.setItem(_HIDDEN_TABS_LS_KEY,JSON.stringify(panels));}catch(e){}
}

function _applyTabVisibility(hidden){
  if(!Array.isArray(hidden)) hidden=[];
  // Hide/unhide all [data-panel] elements (sidebar-nav buttons + rail buttons)
  document.querySelectorAll('[data-panel]').forEach(function(el){
    var panel=el.dataset.panel;
    if(!panel)return;
    var shouldHide=hidden.indexOf(panel)!==-1;
    // Never hide always-visible panels (chat, settings) even if present in hidden_tabs
    if(_ALWAYS_VISIBLE_TABS.has(panel)) shouldHide=false;
    el.classList.toggle('nav-tab-hidden',shouldHide);
  });
  // If the currently active tab is hidden, switch to chat
  var activeRail=document.querySelector('.rail .rail-btn.nav-tab.active[data-panel]');
  var activeSidebar=document.querySelector('.sidebar-nav .nav-tab.active[data-panel]');
  var activeEl=activeRail||activeSidebar;
  if(activeEl&&activeEl.classList.contains('nav-tab-hidden')){
    if(typeof switchPanel==='function') switchPanel('chat');
  }
}

function _renderTabVisibilityChips(){
  var container=$('tabVisibilityChips');
  if(!container)return;
  var hidden=_getHiddenTabs();
  // Scan rail buttons to discover all available panels (skip always-visible + dashboard-link)
  var tabs=document.querySelectorAll('.rail .rail-btn.nav-tab[data-panel]');
  container.innerHTML='';
  tabs.forEach(function(tab){
    var panel=tab.dataset.panel;
    if(!panel||_ALWAYS_VISIBLE_TABS.has(panel))return;
    if(tab.classList.contains('dashboard-link'))return;
    var label=tab.dataset.tooltip||tab.dataset.label||panel;
    // Capitalize first letter
    label=label.charAt(0).toUpperCase()+label.slice(1);
    var chip=document.createElement('button');
    chip.type='button';
    chip.className='tab-visibility-chip';
    var isOff=hidden.indexOf(panel)!==-1;
    if(isOff)chip.classList.add('chip-off');
    chip.textContent=label;
    chip.setAttribute('data-tab-panel',panel);
    // Use role="switch" + aria-checked instead of aria-pressed so screen
    // readers narrate "Tasks switch on/off" (matches user mental model) rather
    // than "Tasks toggle button pressed/not-pressed" (where the polarity is
    // confusing because chip-off looks like the "off" state).
    chip.setAttribute('role','switch');
    chip.setAttribute('aria-checked',isOff?'false':'true');
    chip.onclick=function(){_toggleTabVisibilityChip(panel);};
    container.appendChild(chip);
  });
}

function _toggleTabVisibilityChip(panel){
  if(_ALWAYS_VISIBLE_TABS.has(panel))return;
  var hidden=_getHiddenTabs();
  var idx=hidden.indexOf(panel);
  if(idx!==-1){
    hidden.splice(idx,1);
  }else{
    hidden.push(panel);
  }
  _setHiddenTabs(hidden);
  _applyTabVisibility(hidden);
  _renderTabVisibilityChips();
  _scheduleAppearanceAutosave();
}

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
    avatar_presence_layout: ((($('settingsAvatarPresenceLayout')||{}).value || localStorage.getItem('hermes-avatar-presence-layout') || 'thread')==='composer')?'composer':'thread',
    session_jump_buttons: !!($('settingsSessionJumpButtons')||{}).checked,
    session_endless_scroll: !!($('settingsSessionEndlessScroll')||{}).checked,
    hidden_tabs: _getHiddenTabs(),
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
  _settingsAvatarPresenceLayoutOnOpen=(payload.avatar_presence_layout==='composer')?'composer':'thread';
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
    if(saved&&saved.avatar_presence_layout&&typeof _applyAvatarPresenceLayout==='function'){
      _applyAvatarPresenceLayout(saved.avatar_presence_layout);
      if(typeof _syncAvatarPresencePicker==='function') _syncAvatarPresencePicker(saved.avatar_presence_layout);
      const avatarHidden=$('settingsAvatarPresenceLayout');
      if(avatarHidden) avatarHidden.value=(saved.avatar_presence_layout==='composer')?'composer':'thread';
    }
    if(saved){
      window._sessionJumpButtonsEnabled=!!saved.session_jump_buttons;
      if(typeof _applySessionNavigationPrefs==='function') _applySessionNavigationPrefs();
    }
    window._sessionEndlessScrollEnabled=!!(saved&&saved.session_endless_scroll);
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
  const showQuotaChipCb=$('settingsShowQuotaChip');
  if(showQuotaChipCb) payload.show_quota_chip=showQuotaChipCb.checked;
  const hideSuggestionsCb=$('settingsHideSuggestions');
  if(hideSuggestionsCb) payload.hide_empty_state_suggestions=hideSuggestionsCb.checked;
  const showTpsCb=$('settingsShowTps');
  if(showTpsCb) payload.show_tps=showTpsCb.checked;
  const fadeTextCb=$('settingsFadeTextEffect');
  if(fadeTextCb) payload.fade_text_effect=fadeTextCb.checked;
  const simplifiedToolCb=$('settingsSimplifiedToolCalling');
  if(simplifiedToolCb) payload.simplified_tool_calling=simplifiedToolCb.checked;
  const apiRedactCb=$('settingsApiRedact');
  if(apiRedactCb) payload.api_redact_enabled=apiRedactCb.checked;
  const showCliCb=$('settingsShowCliSessions');
  if(showCliCb) payload.show_cli_sessions=showCliCb.checked;
  const showPreviousMessagingCb=$('settingsShowPreviousMessagingSessions');
  if(showPreviousMessagingCb) payload.show_previous_messaging_sessions=showPreviousMessagingCb.checked;
  const syncCb=$('settingsSyncInsights');
  if(syncCb) payload.sync_to_insights=syncCb.checked;
  const updateCb=$('settingsCheckUpdates');
  if(updateCb) payload.check_for_updates=updateCb.checked;
  const ignoreAgentUpdatesCb=$('settingsIgnoreAgentUpdates');
  if(ignoreAgentUpdatesCb) payload.ignore_agent_updates=ignoreAgentUpdatesCb.checked;
  const whatsNewSummaryCb=$('settingsWhatsNewSummary');
  if(whatsNewSummaryCb) payload.whats_new_summary_enabled=whatsNewSummaryCb.checked;
  const soundCb=$('settingsSoundEnabled');
  if(soundCb) payload.sound_enabled=soundCb.checked;
  const rtlCb=$('settingsRtl');
  if(rtlCb) payload.rtl=rtlCb.checked;
  const notifCb=$('settingsNotificationsEnabled');
  if(notifCb) payload.notifications_enabled=notifCb.checked;
  const sidebarDensitySel=$('settingsSidebarDensity');
  if(sidebarDensitySel) payload.sidebar_density=sidebarDensitySel.value;
  const pinnedLimitField=$('settingsPinnedSessionsLimit');
  if(pinnedLimitField) payload.pinned_sessions_limit=parseInt(pinnedLimitField.value,10);
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
    if(payload&&Object.prototype.hasOwnProperty.call(payload,'fade_text_effect')) window._fadeTextEffect=!!payload.fade_text_effect;
    if(saved&&Object.prototype.hasOwnProperty.call(saved,'pinned_sessions_limit')) window._pinnedSessionsLimit=parseInt(saved.pinned_sessions_limit,10)||3;
    if(payload&&payload.show_tps!==undefined){
      window._showTps=!!(saved&&saved.show_tps);
      if(typeof clearMessageRenderCache==='function') clearMessageRenderCache();
      if(typeof renderMessages==='function') renderMessages();
    }
    if(payload&&payload.hide_empty_state_suggestions!==undefined){
      window._hideEmptyStateSuggestions=!!(saved&&saved.hide_empty_state_suggestions);
      if(typeof applyEmptyStateSuggestionPref==='function') applyEmptyStateSuggestionPref();
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
    const skinVal=(localStorage.getItem('hermes-skin')||settings.skin||'default').toLowerCase();
    const skinSel=$('settingsSkin');
    if(skinSel) skinSel.value=skinVal;
    if(typeof _buildSkinPicker==='function') _buildSkinPicker(skinVal);
    const fontSizeVal=settings.font_size||localStorage.getItem('hermes-font-size')||'default';
    localStorage.setItem('hermes-font-size',fontSizeVal);
    if(typeof _applyFontSize==='function') _applyFontSize(fontSizeVal);
    const fontSizeSel=$('settingsFontSize');
    if(fontSizeSel) fontSizeSel.value=fontSizeVal;
    if(typeof _syncFontSizePicker==='function') _syncFontSizePicker(fontSizeVal);
    const avatarPresenceVal=(settings.avatar_presence_layout==='composer')?'composer':'thread';
    if(typeof _applyAvatarPresenceLayout==='function') _applyAvatarPresenceLayout(avatarPresenceVal);
    const avatarPresenceSel=$('settingsAvatarPresenceLayout');
    if(avatarPresenceSel) avatarPresenceSel.value=avatarPresenceVal;
    if(typeof _syncAvatarPresencePicker==='function') _syncAvatarPresencePicker(avatarPresenceVal);
    const jumpButtonsCb=$('settingsSessionJumpButtons');
    if(jumpButtonsCb){
      jumpButtonsCb.checked=!!settings.session_jump_buttons;
      window._sessionJumpButtonsEnabled=jumpButtonsCb.checked;
      jumpButtonsCb.onchange=function(){
        window._sessionJumpButtonsEnabled=this.checked;
        if(typeof _applySessionNavigationPrefs==='function') _applySessionNavigationPrefs();
        _scheduleAppearanceAutosave();
      };
    }
    if(typeof _applySessionNavigationPrefs==='function') _applySessionNavigationPrefs();
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
    const endlessScrollCb=$('settingsSessionEndlessScroll');
    if(endlessScrollCb){
      endlessScrollCb.checked=!!settings.session_endless_scroll;
      window._sessionEndlessScrollEnabled=endlessScrollCb.checked;
      endlessScrollCb.onchange=function(){
        window._sessionEndlessScrollEnabled=this.checked;
        _scheduleAppearanceAutosave();
      };
    }
    // Tab visibility chips (dynamically populated from DOM)
    var hiddenTabs=[];
    if(Array.isArray(settings.hidden_tabs)){
      // Server value takes priority — even an empty array means "no tabs hidden"
      hiddenTabs=settings.hidden_tabs.filter(function(s){return typeof s==='string'&&s.trim();});
    }else{
      // Server has no hidden_tabs key — fall back to localStorage
      hiddenTabs=_getHiddenTabs();
    }
    _setHiddenTabs(hiddenTabs);
    _applyTabVisibility(hiddenTabs);
    _renderTabVisibilityChips();
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
    // Auxiliary models — load task assignments and provider/model options
    _loadAuxiliaryModels();
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
    // Ambient provider quota chip toggle — default off; only shows at ≥1400px viewport
    // when enabled (see style.css @media (max-width:1399.98px) rule).
    const showQuotaChipCb=$('settingsShowQuotaChip');
    if(showQuotaChipCb){
      showQuotaChipCb.checked=settings.show_quota_chip===true;
      window._showQuotaChip=showQuotaChipCb.checked;
      showQuotaChipCb.addEventListener('change',()=>{
        window._showQuotaChip=showQuotaChipCb.checked;
        if(typeof refreshProviderQuotaIndicator==='function') refreshProviderQuotaIndicator();
        _schedulePreferencesAutosave();
      },{once:false});
    }
    const hideSuggestionsCb=$('settingsHideSuggestions');
    if(hideSuggestionsCb){
      hideSuggestionsCb.checked=settings.hide_empty_state_suggestions===true;
      window._hideEmptyStateSuggestions=hideSuggestionsCb.checked;
      if(typeof applyEmptyStateSuggestionPref==='function') applyEmptyStateSuggestionPref();
      hideSuggestionsCb.addEventListener('change',()=>{
        window._hideEmptyStateSuggestions=hideSuggestionsCb.checked;
        if(typeof applyEmptyStateSuggestionPref==='function') applyEmptyStateSuggestionPref();
        _schedulePreferencesAutosave();
      },{once:false});
    }
    const showTpsCb=$('settingsShowTps');
    if(showTpsCb){showTpsCb.checked=!!settings.show_tps;showTpsCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const pinnedLimitField=$('settingsPinnedSessionsLimit');
    if(pinnedLimitField){
      pinnedLimitField.value=parseInt(settings.pinned_sessions_limit||3,10)||3;
      window._pinnedSessionsLimit=parseInt(pinnedLimitField.value,10)||3;
      pinnedLimitField.addEventListener('change',_schedulePreferencesAutosave,{once:false});
      pinnedLimitField.addEventListener('input',()=>{window._pinnedSessionsLimit=parseInt(pinnedLimitField.value,10)||3;_schedulePreferencesAutosave();},{once:false});
    }
    const fadeTextCb=$('settingsFadeTextEffect');
    if(fadeTextCb){fadeTextCb.checked=!!settings.fade_text_effect;window._fadeTextEffect=fadeTextCb.checked;fadeTextCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const simplifiedToolCb=$('settingsSimplifiedToolCalling');
    if(simplifiedToolCb){simplifiedToolCb.checked=settings.simplified_tool_calling!==false;simplifiedToolCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const apiRedactCb=$('settingsApiRedact');
    if(apiRedactCb){apiRedactCb.checked=settings.api_redact_enabled!==false;apiRedactCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const showCliCb=$('settingsShowCliSessions');
    if(showCliCb){showCliCb.checked=!!settings.show_cli_sessions;showCliCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const showPreviousMessagingCb=$('settingsShowPreviousMessagingSessions');
    if(showPreviousMessagingCb){showPreviousMessagingCb.checked=!!settings.show_previous_messaging_sessions;showPreviousMessagingCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const syncCb=$('settingsSyncInsights');
    if(syncCb){syncCb.checked=!!settings.sync_to_insights;syncCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const updateCb=$('settingsCheckUpdates');
    if(updateCb){updateCb.checked=settings.check_for_updates!==false;updateCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const ignoreAgentUpdatesCb=$('settingsIgnoreAgentUpdates');
    if(ignoreAgentUpdatesCb){ignoreAgentUpdatesCb.checked=!!settings.ignore_agent_updates;ignoreAgentUpdatesCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const whatsNewSummaryCb=$('settingsWhatsNewSummary');
    if(whatsNewSummaryCb){whatsNewSummaryCb.checked=!!settings.whats_new_summary_enabled;whatsNewSummaryCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    const soundCb=$('settingsSoundEnabled');
    if(soundCb){soundCb.checked=!!settings.sound_enabled;soundCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
    // Right-to-left chat layout (#1721 salvage) — Settings-only, no composer button.
    const rtlCb=$('settingsRtl');
    if(rtlCb){
      const saved=!!settings.rtl || localStorage.getItem('hermes-rtl')==='true';
      rtlCb.checked=saved;
      try{localStorage.setItem('hermes-rtl',saved?'true':'false');}catch(_){}
      document.documentElement.classList.toggle('chat-content-rtl',saved);
      rtlCb.addEventListener('change',()=>{
        const on=rtlCb.checked;
        try{localStorage.setItem('hermes-rtl',on?'true':'false');}catch(_){}
        document.documentElement.classList.toggle('chat-content-rtl',on);
        _schedulePreferencesAutosave();
      },{once:false});
    }
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
    list.innerHTML='<div style="color:var(--error);padding:12px;font-size:13px">'+t('plugins_load_failed')+esc(e.message||String(e))+'</div>';
  }
}

function _buildPluginCard(plugin){
  const card=document.createElement('div');
  card.className='provider-card plugin-card';
  card.dataset.plugin=(plugin&&plugin.key)||'';
  // `activation` is the canonical state from /api/plugins (added in #2659).
  // Fall back to the older `enabled` boolean when the field is missing so
  // the panel still works against older backends.
  const activation=(plugin&&typeof plugin.activation==='string')
    ? plugin.activation
    : (plugin&&plugin.enabled===false ? 'disabled' : 'enabled');
  const isProvider=activation==='exclusive'||activation==='provider';
  const hooks=Array.isArray(plugin&&plugin.hooks)?plugin.hooks:[];
  // Provider plugins (memory/web/browser/etc.) register hooks on their
  // category's dispatcher, not the four agent-wide visibility hooks the
  // payload filters to. Show an explanatory line instead of the generic
  // "No registered lifecycle hooks" when the visibility-hook list is empty.
  const hookHtml=hooks.length
    ? hooks.map(h=>`<span class="plugin-hook-badge">${esc(h)}</span>`).join('')
    : '<span class="plugin-hook-empty">'+t(isProvider?'plugins_provider_no_hooks':'plugins_no_hooks')+'</span>';
  const version=(plugin&&plugin.version)?' · v'+esc(plugin.version):'';
  const desc=(plugin&&plugin.description)?esc(plugin.description):t('plugins_no_description');
  let badgeText;
  let badgeClass;
  if(isProvider){
    badgeText=t('plugins_active_provider');
    badgeClass='plugin-card-badge-provider';
  }else if(activation==='enabled'){
    badgeText=t('plugins_enabled');
    badgeClass='';
  }else{
    badgeText=t('plugins_disabled');
    badgeClass='plugin-card-badge-disabled';
  }
  card.innerHTML=`
    <div class="provider-card-header plugin-card-header">
      <div class="provider-card-info">
        <div class="provider-card-name">${esc((plugin&&plugin.name)||t('plugins_unnamed'))}</div>
        <div class="provider-card-meta">${esc((plugin&&plugin.key)||'plugin')}${version}</div>
      </div>
      <span class="provider-card-badge ${badgeClass}">${badgeText}</span>
    </div>
    <div class="provider-card-body plugin-card-body">
      <div class="provider-card-hint">${desc}</div>
      <div class="provider-card-label">${t('plugins_registered_hooks')}</div>
      <div class="plugin-hook-list">${hookHtml}</div>
    </div>
  `;
  return card;
}

// ── Providers panel ───────────────────────────────────────────────────────

const _providerCardEls = new Map(); // providerId → {card, statusDot, input, saveBtn, removeBtn}

async function _fetchProviderQuotaStatus(force=false){
  const endpoint=force?`/api/provider/quota?refresh=1&ts=${Date.now()}`:'/api/provider/quota';
  const status=await api(endpoint,{cache:'no-store'});
  if(status&&typeof status==='object') status.client_fetched_at=new Date().toISOString();
  return status;
}

async function loadProvidersPanel(){
  const list=$('providersList');
  const empty=$('providersEmpty');
  if(!list) return;
  try{
    const data=await api('/api/providers');
    const quota=await _fetchProviderQuotaStatus(false).catch(e=>({ok:false,status:'unavailable',quota:null,message:e.message||t('provider_quota_unavailable'),client_fetched_at:new Date().toISOString()}));
    const providers=(data.providers||[]).filter(p=>p.configurable||p.is_oauth||p.is_custom);
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
    list.innerHTML='<div style="color:var(--error);padding:12px;font-size:13px">Failed to load providers: '+esc(e.message||String(e))+'</div>';
  }
}

async function _refreshProviderQuota(card,button){
  if(!card) return;
  if(button){
    button.disabled=true;
    button.textContent=t('provider_quota_refreshing');
    button.setAttribute('aria-busy','true');
  }
  let failed=false;
  let next;
  try{
    next=await _fetchProviderQuotaStatus(true);
    failed=next&&next.ok===false;
  }catch(e){
    failed=true;
    next={ok:false,status:'unavailable',quota:null,message:e.message||t('provider_quota_unavailable'),client_fetched_at:new Date().toISOString()};
  }
  try{
    const fresh=_buildProviderQuotaCard(next);
    if(fresh){
      card.replaceWith(fresh);
      if(typeof showToast==='function') showToast(failed?t('provider_quota_refresh_failed'):t('provider_quota_refresh_succeeded'));
      return;
    }
  }catch(e){
    failed=true;
  }
  if(card.isConnected&&button){
    button.disabled=false;
    button.textContent=t('provider_quota_refresh_usage');
    button.removeAttribute('aria-busy');
  }
  if(typeof showToast==='function') showToast(t('provider_quota_refresh_failed'));
}

function _formatProviderQuotaMoney(value){
  if(value===null||value===undefined||value==='') return '—';
  const n=Number(value);
  if(!Number.isFinite(n)) return '—';
  return '$'+n.toFixed(2);
}

function _formatProviderQuotaPercent(value){
  if(value===null||value===undefined||value==='') return '—';
  const n=Number(value);
  if(!Number.isFinite(n)) return '—';
  return Math.max(0,Math.min(100,Math.round(n)))+'%';
}

function _formatProviderQuotaReset(value){
  if(!value) return '';
  const d=new Date(value);
  if(Number.isNaN(d.getTime())) return '';
  try{return d.toLocaleString();}catch(e){return value;}
}

function _formatProviderQuotaWindowLabel(accountLimits,w){
  const raw=((w&&w.label)||t('provider_quota_window_fallback')).trim();
  const provider=((accountLimits&&accountLimits.provider)||'').toLowerCase();
  if(provider==='openai-codex'){
    if(raw.toLowerCase()==='session') return t('provider_quota_session_limit');
    if(raw.toLowerCase()==='weekly') return t('provider_quota_weekly_limit');
  }
  return raw||t('provider_quota_window_fallback');
}

function _formatProviderQuotaLastChecked(status){
  const accountLimits=status&&status.account_limits;
  const value=(accountLimits&&accountLimits.fetched_at)||status&&status.client_fetched_at;
  if(!value) return t('provider_quota_last_checked_after_refresh');
  const d=new Date(value);
  if(Number.isNaN(d.getTime())) return t('provider_quota_last_checked_after_refresh');
  try{return t('provider_quota_last_checked',d.toLocaleString());}catch(e){return t('provider_quota_last_checked',value);}
}

function _providerQuotaStateClass(value){
  return String(value||'unavailable').replace(/[^a-z0-9_-]/gi,'').toLowerCase()||'unavailable';
}

function _providerQuotaStatusLabel(value){
  const state=_providerQuotaStateClass(value);
  const key={
    available:'provider_quota_status_available',
    exhausted:'provider_quota_status_exhausted',
    unavailable:'provider_quota_status_unavailable',
    failed:'provider_quota_status_failed',
    checked:'provider_quota_status_checked',
    no_key:'provider_quota_status_no_key',
    invalid_key:'provider_quota_status_invalid_key',
    unsupported:'provider_quota_status_unsupported',
  }[state];
  return key?t(key):state.replace(/_/g,' ');
}

function _providerQuotaWindowMeta(used,reset){
  const meta=[];
  if(used!=='—') meta.push(t('provider_quota_used_meta',used));
  if(reset) meta.push(t('provider_quota_resets_meta',reset));
  return meta;
}

function _providerQuotaRetryAfterText(value){
  const retry=_formatProviderQuotaReset(value);
  return retry?t('provider_quota_retry_after',retry):'';
}

function _providerQuotaUnavailableReason(credential){
  const structured=_providerQuotaRetryAfterText(credential&&credential.retry_after);
  if(structured) return structured;
  const raw=String((credential&&credential.unavailable_reason)||'').trim();
  const match=raw.match(/\bretry after\s+([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+Z?)/i);
  if(match){
    const parsed=_providerQuotaRetryAfterText(match[1]);
    if(parsed) return parsed;
  }
  return raw;
}

function _providerQuotaPoolShouldDefaultOpen(pool){
  try{
    const saved=localStorage.getItem('hermes-provider-quota-pool-open');
    if(saved==='1') return true;
    if(saved==='0') return false;
  }catch(e){}
  const count=Array.isArray(pool&&pool.credentials)?pool.credentials.length:0;
  return count>0&&count<=3;
}

function _buildProviderQuotaPoolBreakdown(accountLimits){
  const pool=accountLimits&&accountLimits.pool;
  if(!pool||!Array.isArray(pool.credentials)||pool.credentials.length===0) return '';
  const defaultOpen=_providerQuotaPoolShouldDefaultOpen(pool);
  const total=Number.isFinite(Number(pool.total_credentials))?Number(pool.total_credentials):pool.credentials.length;
  const available=Number.isFinite(Number(pool.available_credentials))?Number(pool.available_credentials):pool.credentials.filter(c=>c&&c.status==='available').length;
  const exhausted=Number.isFinite(Number(pool.exhausted_credentials))?Number(pool.exhausted_credentials):0;
  const failed=Number.isFinite(Number(pool.failed_credentials))?Number(pool.failed_credentials):0;
  const queried=Number.isFinite(Number(pool.queried_credentials))?Number(pool.queried_credentials):0;
  const summaryParts=[t('provider_quota_pool_summary_available',available,total)];
  if(exhausted>0) summaryParts.push(t('provider_quota_pool_summary_exhausted',exhausted));
  if(failed>0) summaryParts.push(t('provider_quota_pool_summary_failed',failed));
  if(queried>0) summaryParts.push(t('provider_quota_pool_summary_checked',queried));
  const planParts=Array.isArray(pool.plans)?pool.plans.filter(Boolean):[];
  const rows=pool.credentials.map((credential,idx)=>{
    const label=(credential&&credential.label)||t('provider_quota_credential_label',idx+1);
    const status=_providerQuotaStateClass(credential&&credential.status);
    const statusText=_providerQuotaStatusLabel(credential&&credential.status);
    const plan=credential&&credential.plan?` · ${credential.plan}`:'';
    const windows=Array.isArray(credential&&credential.windows)?credential.windows:[];
    const details=Array.isArray(credential&&credential.details)?credential.details.filter(Boolean):[];
    const unavailableReason=_providerQuotaUnavailableReason(credential);
    const windowHtml=windows.length?windows.map(w=>{
      const remaining=_formatProviderQuotaPercent(w&&w.remaining_percent);
      const used=_formatProviderQuotaPercent(w&&w.used_percent);
      const reset=_formatProviderQuotaReset(w&&w.reset_at);
      const meta=_providerQuotaWindowMeta(used,reset);
      const detail=(w&&w.detail)?String(w.detail).trim():'';
      return `<div class="provider-quota-pool-window"><span>${esc(_formatProviderQuotaWindowLabel(accountLimits,w))}</span><strong>${esc(remaining)}</strong>${meta.length?`<small>${esc(meta.join(' · '))}</small>`:''}${detail?`<small class="provider-quota-window-detail">${esc(detail)}</small>`:''}</div>`;
    }).join(''):`<div class="provider-quota-pool-note">${esc(unavailableReason||t('provider_quota_pool_no_windows'))}</div>`;
    const detailHtml=details.length?`<div class="provider-quota-pool-details">${details.map(d=>`<span>${esc(d)}</span>`).join('')}</div>`:'';
    return `
      <div class="provider-quota-pool-row provider-quota-pool-row-${status}">
        <div class="provider-quota-pool-row-head">
          <span>${esc(label)}${esc(plan)}</span>
          <strong>${esc(statusText)}</strong>
        </div>
        <div class="provider-quota-pool-windows">${windowHtml}</div>
        ${detailHtml}
      </div>
    `;
  }).join('');
  const planText=planParts.length?`<div class="provider-quota-pool-plans">${esc(t('provider_quota_pool_plans',planParts.join(', ')))}</div>`:'';
  return `
    <details class="provider-quota-pool"${defaultOpen?' open':''}>
      <summary><span class="provider-quota-pool-summary-label"><span class="provider-quota-pool-chevron" aria-hidden="true"></span><span>${esc(t('provider_quota_credential_pool'))}</span></span><strong>${esc(summaryParts.join(' · '))}</strong></summary>
      ${planText}
      <div class="provider-quota-pool-rows">${rows}</div>
    </details>
  `;
}

function _buildProviderQuotaCard(status){
  if(!status) return null;
  const card=document.createElement('div');
  const state=(status.status||'unavailable').replace(/[^a-z0-9_-]/gi,'').toLowerCase()||'unavailable';
  card.className='provider-quota-card provider-quota-card-'+state;
  const accountLimits=status.account_limits||null;
  const providerBase=status.display_name||status.provider||t('provider_quota_active_provider');
  const provider=(accountLimits&&accountLimits.plan)?`${providerBase} · ${accountLimits.plan}`:providerBase;
  const quota=status.quota||null;
  let body='';
  if(accountLimits&&(status.status==='available'||accountLimits.pool)){
    const windows=Array.isArray(accountLimits.windows)?accountLimits.windows:[];
    const details=Array.isArray(accountLimits.details)&&!accountLimits.pool?accountLimits.details:[];
    const windowHtml=windows.map(w=>{
      const used=_formatProviderQuotaPercent(w&&w.used_percent);
      const reset=_formatProviderQuotaReset(w&&w.reset_at);
      const meta=_providerQuotaWindowMeta(used,reset);
      const detail=(w&&w.detail)?String(w.detail).trim():'';
      return `
        <div class="provider-quota-metric provider-quota-window">
          <span>${esc(_formatProviderQuotaWindowLabel(accountLimits,w))}</span>
          <strong>${esc(_formatProviderQuotaPercent(w&&w.remaining_percent))}</strong>
          ${meta.length?`<small>${esc(meta.join(' · '))}</small>`:''}
          ${detail?`<small class="provider-quota-window-detail">${esc(detail)}</small>`:''}
        </div>
      `;
    }).join('');
    const detailHtml=details.length
      ? `<div class="provider-quota-details">${details.map(d=>`<span>${esc(d)}</span>`).join('')}</div>`
      : '';
    const poolHtml=_buildProviderQuotaPoolBreakdown(accountLimits);
    body=windowHtml+detailHtml+poolHtml;
    if(!body) body=`<div class="provider-quota-message">${esc(status.message||t('provider_quota_account_limits_loaded'))}</div>`;
  }else if(status.status==='available'&&quota){
    body=`
      <div class="provider-quota-metric"><span>${esc(t('provider_quota_metric_remaining'))}</span><strong>${esc(_formatProviderQuotaMoney(quota.limit_remaining))}</strong></div>
      <div class="provider-quota-metric"><span>${esc(t('provider_quota_metric_used'))}</span><strong>${esc(_formatProviderQuotaMoney(quota.usage))}</strong></div>
      <div class="provider-quota-metric"><span>${esc(t('provider_quota_metric_limit'))}</span><strong>${esc(_formatProviderQuotaMoney(quota.limit))}</strong></div>
    `;
  }else{
    body=`<div class="provider-quota-message">${esc(status.message||t('provider_quota_unavailable'))}</div>`;
  }
  card.innerHTML=`
    <div class="provider-quota-header">
      <div>
        <div class="provider-quota-title">${esc(t('provider_quota_title'))}</div>
        <div class="provider-quota-subtitle">${esc(provider)}</div>
        <div class="provider-quota-checked">${esc(_formatProviderQuotaLastChecked(status))}</div>
      </div>
      <div class="provider-quota-actions">
        <span class="provider-quota-badge">${esc(_providerQuotaStatusLabel(state))}</span>
        <button class="provider-quota-refresh" type="button" data-provider-quota-refresh title="${esc(t('provider_quota_refresh_title'))}">${esc(t('provider_quota_refresh_usage'))}</button>
      </div>
    </div>
    <div class="provider-quota-body">${body}</div>
  `;
  const refreshBtn=card.querySelector('[data-provider-quota-refresh]');
  if(refreshBtn) refreshBtn.addEventListener('click',()=>_refreshProviderQuota(card,refreshBtn));
  const poolDetails=card.querySelector('.provider-quota-pool');
  if(poolDetails){
    poolDetails.addEventListener('toggle',()=>{
      try{localStorage.setItem('hermes-provider-quota-pool-open',poolDetails.open?'1':'0');}catch(e){}
    });
  }
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

  let input=null;
  let saveBtn=null;
  if(p.configurable){
    const field=document.createElement('div');
    field.className='provider-card-field';
    const label=document.createElement('label');
    label.className='provider-card-label';
    label.textContent=t('providers_status_api_key');
    field.appendChild(label);

    const row=document.createElement('div');
    row.className='provider-card-row';
    input=document.createElement('input');
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
    saveBtn=document.createElement('button');
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
  }else{
    const hint=document.createElement('div');
    hint.className='provider-card-hint';
    hint.textContent=p.is_custom
      ? 'Custom provider loaded from config.yaml / hermes model. Edit it from the CLI or config file.'
      : 'Provider is managed outside the WebUI.';
    body.appendChild(hint);
  }

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

  if(input&&saveBtn){
    _providerCardEls.set(p.id,{card,input,saveBtn,hasKey:p.has_key});
    input.addEventListener('input',()=>{saveBtn.disabled=!input.value.trim();});
  }
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
    // Fire-and-forget: don't block the providers panel refresh on a
    // dropdown rebuild. The composer/Settings dropdowns will catch up
    // on the very next paint frame.
    if(typeof window._ensureModelDropdownReady==='function'){
      window._modelDropdownReady=null;
      Promise.resolve(window._ensureModelDropdownReady()).catch(()=>{});
    }else if(typeof populateModelDropdown==='function'){
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
  const {sendKey,showTokenUsage,showQuotaChip,showTps,fadeTextEffect,showCliSessions,theme,skin,language,sidebarDensity,fontSize,avatarPresenceLayout}=opts;
  window._sendKey=sendKey||'enter';
  window._showTokenUsage=showTokenUsage;
  window._showQuotaChip=showQuotaChip===true;
  window._showTps=showTps;
  window._fadeTextEffect=!!fadeTextEffect;
  window._showCliSessions=showCliSessions;
  window._showPreviousMessagingSessions=!!body.show_previous_messaging_sessions;
  window._soundEnabled=body.sound_enabled;
  window._notificationsEnabled=body.notifications_enabled;
  window._whatsNewSummaryEnabled=!!body.whats_new_summary_enabled;
  window._showThinking=body.show_thinking!==false;
  window._simplifiedToolCalling=body.simplified_tool_calling!==false;
  window._sessionJumpButtonsEnabled=!!body.session_jump_buttons;
  if(typeof _applySessionNavigationPrefs==='function') _applySessionNavigationPrefs();
  window._sidebarDensity=sidebarDensity==='detailed'?'detailed':'compact';
  window._busyInputMode=body.busy_input_mode||'queue';
  window._sessionEndlessScrollEnabled=!!body.session_endless_scroll;
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
  _settingsAvatarPresenceLayoutOnOpen=(avatarPresenceLayout==='composer')?'composer':'thread';
  if(typeof _applyAvatarPresenceLayout==='function') _applyAvatarPresenceLayout(_settingsAvatarPresenceLayoutOnOpen);
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
    const data=await api('/api/updates/check?force=1',{timeoutMs:60000});
    if(data.disabled){
      if(status){status.textContent=t('settings_updates_disabled');status.style.color='var(--muted)';}
    } else {
      const errorParts=[];
      const formatUpdateError=(typeof _formatUpdateCheckError==='function')
        ? _formatUpdateCheckError
        : ((label,info)=>info&&info.error?label:null);
      const webuiError=formatUpdateError('WebUI',data.webui);
      const agentError=formatUpdateError('Agent',data.agent);
      if(webuiError) errorParts.push(webuiError);
      if(agentError) errorParts.push(agentError);
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
      } else if(errorParts.length){
        if(status){status.textContent=t('settings_update_check_failed')+': '+errorParts.join(', ');status.style.color='var(--error)';}
      } else {
        if(status){status.textContent=t('settings_up_to_date');status.style.color='var(--success)';}
        if(typeof _showUpdateBanner==='function') _showUpdateBanner(data);
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

// ── Auxiliary Models ──────────────────────────────────────────────────────────

// Canonical auxiliary task slots with display names.
// Keep in sync with hermes_cli/main.py _AUX_TASKS and hermes_cli/web_server.py _AUX_TASK_SLOTS.
const _AUX_TASK_SLOTS=[
 {key:'vision',name:'Vision',desc:'image/screenshot analysis'},
 {key:'compression',name:'Compression',desc:'context summarization'},
 {key:'web_extract',name:'Web extract',desc:'web page summarization'},
 {key:'session_search',name:'Session search',desc:'past-conversation recall'},
 {key:'approval',name:'Approval',desc:'smart command approval'},
 {key:'mcp',name:'MCP',desc:'MCP tool reasoning'},
 {key:'title_generation',name:'Title generation',desc:'session titles'},
 {key:'skills_hub',name:'Skills hub',desc:'skills search/install'},
 {key:'curator',name:'Curator',desc:'skill-usage review pass'},
];

let _auxProviders=[];       // cached provider list from /api/model/options
let _auxOriginalConfig=null; // snapshot of initial config for dirty detection

function _auxSelectStyle(){
 return 'width:100%;padding:6px 8px;background:var(--code-bg);color:var(--text);border:1px solid var(--border2);border-radius:6px;font-size:12px;box-sizing:border-box';
}

function _buildAuxProviderOptions(sel,providers,currentProvider){
 sel.innerHTML='';
 // "auto" = use main model
 const autoOpt=document.createElement('option');
 autoOpt.value='auto';autoOpt.textContent='auto ('+t('settings_aux_provider_auto')+')';
 if(currentProvider==='auto'||!currentProvider) autoOpt.selected=true;
 sel.appendChild(autoOpt);
 for(const p of providers){
  const opt=document.createElement('option');
  opt.value=p.slug;opt.textContent=p.name;
  if(p.slug===currentProvider) opt.selected=true;
  sel.appendChild(opt);
 }
}

function _buildAuxModelOptions(sel,provider,providers,currentModel){
 sel.innerHTML='';
 const emptyOpt=document.createElement('option');
 emptyOpt.value='';emptyOpt.textContent=t('settings_aux_model_auto')||'auto (use provider default)';
 sel.appendChild(emptyOpt);
 if(!provider||provider==='auto'){
  sel.value=currentModel||'';
  return;
 }
 // Find matching provider in cached list
 const pData=providers.find(p=>p.slug===provider);
 if(pData&&pData.models){
  for(const mId of pData.models){
   const opt=document.createElement('option');
   opt.value=mId;opt.textContent=mId;
   if(mId===currentModel) opt.selected=true;
   sel.appendChild(opt);
  }
 }
 // Always allow custom model — add a text input option hint
 const customOpt=document.createElement('option');
 customOpt.value='__custom__';customOpt.textContent=t('settings_aux_model_custom')||'Custom model…';
 sel.appendChild(customOpt);
 // If currentModel not in list and not empty, add it as a custom option
 if(currentModel&&!pData?.models?.includes(currentModel)){
  const existingOpt=document.createElement('option');
  existingOpt.value=currentModel;existingOpt.textContent=currentModel+' (configured)';
  existingOpt.selected=true;
  sel.insertBefore(existingOpt,customOpt);
 }
}

function _onAuxProviderChange(taskKey,providers){
 const provSel=$('aux-prov-'+taskKey);
 const modelSel=$('aux-model-'+taskKey);
 if(!provSel||!modelSel) return;
 const provider=provSel.value;
 _buildAuxModelOptions(modelSel,provider,providers,'');
 _markAuxDirty();
}

async function _onAuxModelChange(taskKey){
 const modelSel=$('aux-model-'+taskKey);
 if(!modelSel) return;
 if(modelSel.value==='__custom__'){
  const customModel=await showPromptDialog({title:t('settings_aux_model_custom')||'Custom model',message:t('settings_aux_model_custom_prompt')||'Enter model ID:',placeholder:'model/provider:model-id',confirmLabel:t('settings_btn_apply_aux_models')||'Apply'});
  if(customModel&&customModel.trim()){
   // Insert custom model option before the __custom__ option
   const opt=document.createElement('option');
   opt.value=customModel.trim();opt.textContent=customModel.trim();
   // Remove __custom__ selection
   const customIdx=[...modelSel.options].findIndex(o=>o.value==='__custom__');
   if(customIdx>=0) modelSel.insertBefore(opt,modelSel.options[customIdx]);
   modelSel.value=customModel.trim();
  }else{
   modelSel.value='';
  }
 }
 _markAuxDirty();
}

function _markAuxDirty(){
 const applyBtn=$('btnApplyAuxModels');
 if(applyBtn) applyBtn.style.display='';
 _markSettingsDirty();
}

async function _loadAuxiliaryModels(){
 const container=$('auxModelsContainer');
 if(!container) return;
 container.innerHTML='<div style="color:var(--muted);font-size:12px">'+(t('settings_aux_loading')||'Loading…')+'</div>';

 try{
 // Fetch auxiliary config AND the WebUI's own /api/models for provider/model lists
 const [auxData,modelsData]=await Promise.all([
 api('/api/model/auxiliary').catch(()=>null),
 api('/api/models').catch(()=>null),
 ]);
 // Build provider list from /api/models groups
 // /api/models returns: { groups: [{ provider: str, provider_id: str, models: [{id,label}] }] }
 const groups=(modelsData&&modelsData.groups)||[];
 _auxProviders=groups.filter(g=>g.provider&&g.models&&g.models.length>0).map(g=>({
 slug:g.provider_id||g.provider,
 name:g.provider,
 models:g.models.map(m=>m.id),
 }));
 const tasks=(auxData&&auxData.tasks)||[];
  // Build a quick lookup: taskKey → {provider, model}
  const taskMap={};
  for(const t of tasks) taskMap[t.task]=t;
  _auxOriginalConfig=JSON.parse(JSON.stringify(taskMap));

  container.innerHTML='';
  for(const slot of _AUX_TASK_SLOTS){
   const cfg=taskMap[slot.key]||{provider:'auto',model:''};
   const row=document.createElement('div');
   row.style.cssText='display:grid;grid-template-columns:120px 1fr 1fr;gap:8px;align-items:center;margin-bottom:8px';

   // Task name + description
   const label=document.createElement('div');
   label.style.cssText='font-size:12px;font-weight:500;color:var(--text);line-height:1.3';
   label.innerHTML=esc(slot.name)+'<div style="font-size:10px;color:var(--muted);font-weight:400">'+esc(slot.desc)+'</div>';
   row.appendChild(label);

   // Provider select
   const provSel=document.createElement('select');
   provSel.id='aux-prov-'+slot.key;
   provSel.style.cssText=_auxSelectStyle();
   _buildAuxProviderOptions(provSel,_auxProviders,cfg.provider);
   provSel.addEventListener('change',()=>_onAuxProviderChange(slot.key,_auxProviders));
   row.appendChild(provSel);

   // Model select
   const modelSel=document.createElement('select');
   modelSel.id='aux-model-'+slot.key;
   modelSel.style.cssText=_auxSelectStyle();
   _buildAuxModelOptions(modelSel,cfg.provider,_auxProviders,cfg.model);
   modelSel.addEventListener('change',()=>_onAuxModelChange(slot.key));
   row.appendChild(modelSel);

   container.appendChild(row);
  }
  // Hide apply button (no changes yet)
  const applyBtn=$('btnApplyAuxModels');
  if(applyBtn) applyBtn.style.display='none';

  // Reset button
  const resetBtn=$('btnResetAuxModels');
  if(resetBtn&&!resetBtn._bound){
   resetBtn._bound=true;
   resetBtn.addEventListener('click',async()=>{
    if(!(await showConfirmDialog({title:t('settings_aux_reset_confirm_title')||'Reset auxiliary models?',message:t('settings_aux_reset_confirm_msg')||'This will set all auxiliary tasks to auto (use main model).',confirmLabel:t('settings_btn_reset_aux_models')||'Reset',danger:true}))) return;
    try{
     await api('/api/model/set',{method:'POST',body:JSON.stringify({scope:'auxiliary',task:'__reset__',provider:'auto',model:''})});
     if(typeof showToast==='function') showToast(t('settings_aux_reset_done')||'Auxiliary models reset to auto');
     _loadAuxiliaryModels();
    }catch(e){
     if(typeof showToast==='function') showToast(t('settings_aux_save_failed')||'Failed to reset auxiliary models');
    }
   });
  }

  // Apply button
  if(applyBtn&&!applyBtn._bound){
   applyBtn._bound=true;
   applyBtn.addEventListener('click',_applyAuxModels);
  }
 }catch(e){
  console.warn('[settings] auxiliary models load failed',e);
  container.innerHTML='<div style="color:var(--muted);font-size:12px">'+(t('settings_aux_load_failed')||'Could not load auxiliary model settings. Make sure the agent API is available.')+'</div>';
 }
}

async function _applyAuxModels(){
 let saved=0;
 for(const slot of _AUX_TASK_SLOTS){
  const provSel=$('aux-prov-'+slot.key);
  const modelSel=$('aux-model-'+slot.key);
  if(!provSel) continue;
  const provider=provSel.value;
  const model=(modelSel&&modelSel.value!=='__custom__')?(modelSel.value||''):'';
  const orig=_auxOriginalConfig?.[slot.key]||{provider:'auto',model:''};
  // Only save if changed
  if(provider!==orig.provider||model!==orig.model){
   try{
    await api('/api/model/set',{method:'POST',body:JSON.stringify({scope:'auxiliary',task:slot.key,provider,model})});
    saved++;
   }catch(e){
    console.warn('[settings] failed to save aux task',slot.key,e);
    if(typeof showToast==='function') showToast(t('settings_aux_save_failed')||'Failed to save auxiliary model for '+slot.name);
    return;
   }
  }
 }
 if(typeof showToast==='function') showToast(saved?(t('settings_aux_saved')||'Auxiliary models updated'):(t('settings_aux_no_changes')||'No changes to apply'));
 // Reload to refresh state
 _loadAuxiliaryModels();
}

async function saveSettings(andClose){
  const model=($('settingsModel')||{}).value;
  const modelChanged=(model||'')!==(_settingsHermesDefaultModelOnOpen||'');
  const sendKey=($('settingsSendKey')||{}).value;
  const showTokenUsage=!!($('settingsShowTokenUsage')||{}).checked;
  const showQuotaChip=!!($('settingsShowQuotaChip')||{}).checked;
  const showTps=!!($('settingsShowTps')||{}).checked;
  const fadeTextEffect=!!($('settingsFadeTextEffect')||{}).checked;
  const showCliSessions=!!($('settingsShowCliSessions')||{}).checked;
  const showPreviousMessagingSessions=!!($('settingsShowPreviousMessagingSessions')||{}).checked;
  const pinnedSessionsLimit=parseInt(($('settingsPinnedSessionsLimit')||{}).value,10)||3;
  const pw=($('settingsPassword')||{}).value;
  const theme=($('settingsTheme')||{}).value||'dark';
  const skin=($('settingsSkin')||{}).value||'default';
  const fontSize=($('settingsFontSize')||{}).value||localStorage.getItem('hermes-font-size')||'default';
  const avatarPresenceLayout=(($('settingsAvatarPresenceLayout')||{}).value==='composer')?'composer':'thread';
  const language=($('settingsLanguage')||{}).value||'en';
  const sidebarDensity=($('settingsSidebarDensity')||{}).value==='detailed'?'detailed':'compact';
  const busyInputMode=($('settingsBusyInputMode')||{}).value||'queue';
  const body={};

  if(sendKey) body.send_key=sendKey;
  body.theme=theme;
  body.skin=skin;
  body.font_size=fontSize;
  body.avatar_presence_layout=avatarPresenceLayout;
  body.session_jump_buttons=!!($('settingsSessionJumpButtons')||{}).checked;
  body.session_endless_scroll=!!($('settingsSessionEndlessScroll')||{}).checked;
  body.language=language;
  body.show_token_usage=showTokenUsage;
  body.show_quota_chip=showQuotaChip===true;
  body.show_tps=showTps;
  body.fade_text_effect=fadeTextEffect;
  body.simplified_tool_calling=!!($('settingsSimplifiedToolCalling')||{}).checked;
  body.api_redact_enabled=!!($('settingsApiRedact')||{}).checked;
  body.show_cli_sessions=showCliSessions;
  body.show_previous_messaging_sessions=showPreviousMessagingSessions;
  body.pinned_sessions_limit=pinnedSessionsLimit;
  body.sync_to_insights=!!($('settingsSyncInsights')||{}).checked;
  body.check_for_updates=!!($('settingsCheckUpdates')||{}).checked;
  body.ignore_agent_updates=!!($('settingsIgnoreAgentUpdates')||{}).checked;
  body.whats_new_summary_enabled=!!($('settingsWhatsNewSummary')||{}).checked;
  body.sound_enabled=!!($('settingsSoundEnabled')||{}).checked;
  body.rtl=!!($('settingsRtl')||{}).checked;
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
      _applySavedSettingsUi(saved, body, {sendKey,showTokenUsage,showQuotaChip,showTps,fadeTextEffect,showCliSessions,theme,skin,language,sidebarDensity,fontSize,avatarPresenceLayout});
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
    _applySavedSettingsUi(saved, body, {sendKey,showTokenUsage,showQuotaChip,showTps,fadeTextEffect,showCliSessions,theme,skin,language,sidebarDensity,fontSize,avatarPresenceLayout});
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
          if(c.toast_notifications !== false){
            showToast(t('cron_completion_status', c.name, c.status==='error' ? t('status_failed') : t('status_completed')),4000);
          }
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
switchPanel=async function(name,opts){ return _origSwitchPanel(name,opts); };

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
function toggleMcpServer(name, enabled){
  api('/api/mcp/servers/'+encodeURIComponent(name),{
    method:'PATCH',
    body:JSON.stringify({enabled:enabled}),
  }).then(r=>{
    if(r&&r.ok) showToast(t(enabled?'mcp_enabled_toast':'mcp_disabled_toast',name));
    else showToast(t('mcp_toggle_failed'),'error');
    loadMcpServers();
  }).catch(()=>{showToast(t('mcp_toggle_failed'),'error');loadMcpServers();});
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
      const isEnabled=s.enabled!==false;
      const encodedName=encodeURIComponent(s.name).replace(/'/g,"\\'");
      const toggleBtn=r.toggle_supported
        ?`<button type="button" class="mcp-toggle-btn ${isEnabled?'mcp-toggle-enabled':'mcp-toggle-disabled'}" title="${esc(t(isEnabled?'mcp_disable_server':'mcp_enable_server'))}" onclick="toggleMcpServer('${encodedName}',${!isEnabled})">${esc(t(isEnabled?'mcp_enabled_yes':'mcp_enabled_no'))}</button>`
        :`<span>${esc(t(isEnabled?'mcp_enabled_yes':'mcp_enabled_no'))}</span>`;
      return `<div class="mcp-server-row">
        <div class="mcp-server-row-head">
          <span class="mcp-server-name">${esc(s.name)}</span>
          ${transportBadge}
          ${statusBadge}
        </div>
        <div class="mcp-server-detail">${esc(detail)}${secretInfo?' | '+esc(secretInfo):''}</div>
        <div class="mcp-server-meta"><span class="mcp-tool-count">${esc(t('mcp_tool_count',toolCount))}</span>${toggleBtn}</div>
      </div>`;
    }).join('');
  }).catch(()=>{list.innerHTML=`<div class="mcp-error-state" style="color:#ef4444;font-size:12px;padding:6px 0">${esc(t('mcp_load_failed'))}</div>`});
}
let _mcpToolsCache=[];
let _mcpToolsMeta={};
let _mcpToolsPage=1;
let _mcpToolsPageSize=5;
const MCP_TOOLS_PAGE_SIZE_OPTIONS=[5,10,20,40];
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
function _mcpToolsSummary(total, filtered, page, pages, query){
  const trimmedQuery=(query||'').trim();
  if(!filtered){
    if(trimmedQuery) return t('mcp_tools_summary_no_matches',trimmedQuery,total);
    return total?t('mcp_tools_summary_none'):'';
  }
  const pageSize=_mcpToolsPageSize||5;
  const start=(page-1)*pageSize+1;
  const end=Math.min(filtered,page*pageSize);
  const searchNote=trimmedQuery?t('mcp_tools_summary_matching',trimmedQuery):'';
  const totalNote=filtered===total?'':t('mcp_tools_summary_total_note',total);
  return t('mcp_tools_summary_showing',start,end,filtered,searchNote,totalNote,page,pages);
}
function _mcpToolPageSizeControl(){
  const options=MCP_TOOLS_PAGE_SIZE_OPTIONS.map(size=>`<option value="${size}" ${size===_mcpToolsPageSize?'selected':''}>${size}</option>`).join('');
  return `<label class="mcp-tool-page-size">${esc(t('mcp_tools_page_size_prefix'))} <select aria-label="${esc(t('mcp_tools_per_page_aria'))}" onchange="setMcpToolsPageSize(this.value)">${options}</select> ${esc(t('mcp_tools_page_size_suffix'))}</label>`;
}
function _mcpToolsEmptyMessage(query){
  const base=esc(t(query?'mcp_tools_no_matches':'mcp_tools_no_tools'));
  const unavailable=Array.isArray(_mcpToolsMeta.unavailable_servers)?_mcpToolsMeta.unavailable_servers:[];
  if(query||!unavailable.length) return base;
  return `${base}<br><span class="mcp-tool-empty-detail">${esc(t('mcp_tools_inactive_configured_servers',unavailable.join(', ')))}</span>`;
}
function _renderMcpToolPager(filteredCount, page, pages){
  const pager=$('mcpToolPager');
  if(!pager) return;
  if(pages<=1){
    pager.innerHTML='';
    return;
  }
  pager.innerHTML=`<button type="button" class="mcp-tool-page-btn" onclick="setMcpToolsPage(${page-1})" ${page<=1?'disabled':''} aria-label="${esc(t('mcp_tools_previous_page_aria'))}">${esc(t('mcp_tools_previous_page'))}</button>
    <span class="mcp-tool-page-label">${page} / ${pages}</span>
    <button type="button" class="mcp-tool-page-btn" onclick="setMcpToolsPage(${page+1})" ${page>=pages?'disabled':''} aria-label="${esc(t('mcp_tools_next_page_aria'))}">${esc(t('mcp_tools_next_page'))}</button>`;
}
function _renderMcpTools(tools, query){
  const list=$('mcpToolList');
  const toolbar=$('mcpToolToolbar');
  if(!list) return;
  const filtered=_filterMcpToolsForSearch(tools, query);
  const total=Array.isArray(tools)?tools.length:0;
  const pages=Math.max(1,Math.ceil(filtered.length/_mcpToolsPageSize));
  _mcpToolsPage=Math.min(Math.max(1,_mcpToolsPage||1),pages);
  if(toolbar) toolbar.innerHTML=`<span class="mcp-tool-summary">${esc(_mcpToolsSummary(total,filtered.length,_mcpToolsPage,pages,query))}</span>${_mcpToolPageSizeControl()}`;
  _renderMcpToolPager(filtered.length,_mcpToolsPage,pages);
  if(!filtered.length){
    list.innerHTML=`<div class="mcp-tool-empty-state" style="color:var(--muted);font-size:12px;padding:6px 0">${_mcpToolsEmptyMessage(query)}</div>`;
    return;
  }
  const visible=filtered.slice((_mcpToolsPage-1)*_mcpToolsPageSize,_mcpToolsPage*_mcpToolsPageSize);
  list.innerHTML=visible.map(tool=>{
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
function setMcpToolsPage(page){
  _mcpToolsPage=page;
  const input=$('mcpToolSearch');
  _renderMcpTools(_mcpToolsCache,input?input.value:'');
  const list=$('mcpToolList');
  if(list) list.scrollTop=0;
}
function setMcpToolsPageSize(size){
  const next=Number(size);
  if(!MCP_TOOLS_PAGE_SIZE_OPTIONS.includes(next)) return;
  _mcpToolsPageSize=next;
  _mcpToolsPage=1;
  const input=$('mcpToolSearch');
  _renderMcpTools(_mcpToolsCache,input?input.value:'');
  const list=$('mcpToolList');
  if(list) list.scrollTop=0;
}
function filterMcpTools(){
  _mcpToolsPage=1;
  const input=$('mcpToolSearch');
  _renderMcpTools(_mcpToolsCache,input?input.value:'');
  const list=$('mcpToolList');
  if(list) list.scrollTop=0;
}
function loadMcpTools(){
  const list=$('mcpToolList');
  const toolbar=$('mcpToolToolbar');
  const pager=$('mcpToolPager');
  if(!list) return;
  if(toolbar) toolbar.textContent='';
  if(pager) pager.innerHTML='';
  list.innerHTML=`<div style="color:var(--muted);font-size:12px;padding:6px 0">${esc(t('loading'))}</div>`;
  api('/api/mcp/tools').then(r=>{
    _mcpToolsCache=(r&&Array.isArray(r.tools))?r.tools:[];
    _mcpToolsMeta=r||{};
    _mcpToolsPage=1;
    filterMcpTools();
  }).catch(()=>{list.innerHTML=`<div class="mcp-tool-error-state" style="color:#ef4444;font-size:12px;padding:6px 0">${esc(t('mcp_tools_load_failed'))}</div>`});
}
function loadGatewayStatus(){
  const card=$('gatewayStatusCard');
  if(!card) return;
  api('/api/gateway/status').then(r=>{
    if(!r) return;
    if(!r.configured){
      card.innerHTML=`<div style="color:var(--muted);font-size:12px;display:flex;align-items:center;gap:6px"><span style="width:8px;height:8px;border-radius:50%;background:#f59e0b;display:inline-block"></span>Gateway not configured</div>`;
      return;
    }
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
