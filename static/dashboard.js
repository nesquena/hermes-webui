let _dashboardLoaded = false;
let chatMessagesAnchor = null;
let chatComposerAnchor = null;
let dashboardHealthPollTimer = null;
let dashboardHealthCache = { at: 0, system: null, vps: null };
let dashboardSearchTimer = null;

const DASHBOARD_FALLBACK_KPIS = [
  { id: 'active_projects', label_key: 'kpi_active_projects', value: 0, delta_key: 'kpi_delta_this_month', delta_value: 0, panel: 'projects' },
  { id: 'tasks_in_progress', label_key: 'kpi_tasks_in_progress', value: 0, delta_key: 'kpi_delta_since_yesterday', delta_value: 0, panel: 'projects' },
  { id: 'completed', label_key: 'kpi_completed', value: 0, delta_key: 'kpi_delta_this_week', delta_value: 0, panel: 'projects' },
  { id: 'agents_online', label_key: 'kpi_agents_online', value: 1, delta_key: 'kpi_all_operational', delta_value: null, panel: 'skills' },
];

const DASHBOARD_QUICK_ACTION_PROMPTS = {
  new_project: 'Quero criar um novo projeto. Me ajude a definir nome, objetivo, tarefas iniciais e próximos passos.',
  new_document: 'Quero criar um novo documento. Me ajude a estruturar o conteúdo e os tópicos principais.',
  new_component: 'Quero criar um novo componente. Me ajude a definir escopo, estados e comportamento esperado.',
  generate_report: 'Quero gerar um relatório do projeto. Me ajude a resumir status, progresso, riscos e próximos passos.',
  deploy_project: 'Quero preparar o deploy do projeto. Me ajude a montar um checklist seguro de publicação.',
};

function _getGreetingKey() {
  const h = new Date().getHours();
  if (h >= 5 && h < 12) return 'greeting_good_morning';
  if (h >= 12 && h < 18) return 'greeting_good_afternoon';
  return 'greeting_good_evening';
}

function _t(key) {
  if (typeof t === 'function') return t(key);
  const lang = (localStorage.getItem('hermes-lang') || 'en');
  if (typeof TRANSLATIONS === 'object' && TRANSLATIONS[lang]) return TRANSLATIONS[lang][key] || key;
  return key;
}

function _tf(key, value) {
  if (typeof t === 'function') return t(key, value);
  const raw = _t(key);
  return String(raw).replace('{0}', value);
}

function _formatKpiValue(value) {
  const n = Number(value || 0);
  return Number.isFinite(n) ? n.toLocaleString() : '0';
}

function renderDashboardKpis(cards) {
  const grid = document.getElementById('dashboardKpiGrid');
  if (!grid) return;
  const items = Array.isArray(cards) && cards.length ? cards : DASHBOARD_FALLBACK_KPIS;
  grid.innerHTML = '';
  items.slice(0, 4).forEach(card => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'dashboard-kpi-card';
    btn.setAttribute('data-kpi-id', card.id || '');
    btn.addEventListener('click', () => {
      if (card.panel && typeof switchPanel === 'function') switchPanel(card.panel);
    });

    const label = document.createElement('div');
    label.className = 'dashboard-kpi-label';
    label.textContent = _t(card.label_key || '');

    const value = document.createElement('div');
    value.className = 'dashboard-kpi-value';
    value.textContent = _formatKpiValue(card.value);

    const delta = document.createElement('div');
    delta.className = 'dashboard-kpi-delta';
    delta.textContent = '✓ ' + _tf(card.delta_key || '', card.delta_value ?? '');

    btn.append(label, value, delta);
    grid.appendChild(btn);
  });
}

function _insertAfter(anchor, node) {
  if (!anchor || !anchor.parentNode || !node) return;
  anchor.parentNode.insertBefore(node, anchor.nextSibling);
}

// ── Mobile pull-to-refresh on the chat transcript ──
// Activates only on touch viewports (the CSS media query also gates the
// indicator to hover:none widths). When the user pulls down at scrollTop=0
// of the #messages container, the indicator follows the finger; releasing
// past PULL_THRESHOLD calls refreshSession(). Bypasses native browser
// pull-to-refresh because we preventDefault on touchmove while pulling,
// matching the behaviour the user expects from Claude / ChatGPT mobile.
const PULL_REFRESH_THRESHOLD = 70;   // px the user must pull before we trigger
const PULL_REFRESH_DAMPING = 0.5;    // resist past the threshold so the indicator can't run away
const PULL_REFRESH_MAX = 130;        // visual ceiling for the indicator
let _pullRefreshState = null;
function _pullRefreshSetTransform(indicator, distance) {
  if (!indicator) return;
  const clamped = Math.max(0, Math.min(distance, PULL_REFRESH_MAX));
  indicator.style.transform = `translate3d(0, ${clamped}px, 0)`;
}
function _pullRefreshReset(indicator, withDelay = true) {
  if (!indicator) return;
  if (withDelay) {
    indicator.style.transition = 'transform .2s ease, opacity .2s ease';
    setTimeout(() => {
      indicator.style.transition = '';
      indicator.style.transform = '';
    }, 220);
  } else {
    indicator.style.transform = '';
  }
  indicator.classList.remove('is-active');
  indicator.classList.remove('is-refreshing');
  const labelEl = indicator.querySelector('.pull-refresh-label');
  if (labelEl) labelEl.textContent = (typeof t === 'function') ? t('pull_to_refresh') : 'Pull to refresh';
}
function _isTouchInsideComposer(target) {
  return !!(target && target.closest && (target.closest('.composer-wrap') || target.closest('.composer-box')));
}
function _initPullToRefresh() {
  const messages = document.getElementById('messages');
  const indicator = document.getElementById('pullRefreshIndicator');
  if (!messages || !indicator) return;
  // Only wire up on touch viewports — desktop mouse drag should not trigger
  // a refresh. The hover:none media query also hides the indicator visually,
  // so this is belt-and-braces.
  if (!('ontouchstart' in window) && !(navigator.maxTouchPoints > 0)) return;

  messages.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) return;
    if (_isTouchInsideComposer(e.target)) return;
    if (messages.scrollTop > 0) return;
    _pullRefreshState = {
      startY: e.touches[0].clientY,
      lastDelta: 0,
      triggered: false,
    };
  }, { passive: true });

  messages.addEventListener('touchmove', (e) => {
    if (!_pullRefreshState) return;
    if (messages.scrollTop > 0) {
      // The list scrolled away from the top mid-gesture — abort the pull.
      _pullRefreshReset(indicator, false);
      _pullRefreshState = null;
      return;
    }
    const delta = e.touches[0].clientY - _pullRefreshState.startY;
    if (delta <= 0) return;
    // Past threshold, dampen so the indicator stops keeping up 1:1 with the finger.
    const visual = delta < PULL_REFRESH_THRESHOLD
      ? delta
      : PULL_REFRESH_THRESHOLD + (delta - PULL_REFRESH_THRESHOLD) * PULL_REFRESH_DAMPING;
    _pullRefreshState.lastDelta = visual;
    indicator.classList.add('is-active');
    _pullRefreshSetTransform(indicator, visual);
    // Block native browser pull-to-refresh while we're handling the gesture.
    if (e.cancelable) e.preventDefault();
  }, { passive: false });

  const onEnd = () => {
    if (!_pullRefreshState) return;
    const reached = _pullRefreshState.lastDelta >= PULL_REFRESH_THRESHOLD;
    const state = _pullRefreshState;
    _pullRefreshState = null;
    if (!reached || state.triggered) {
      _pullRefreshReset(indicator, true);
      return;
    }
    state.triggered = true;
    indicator.classList.add('is-refreshing');
    const labelEl = indicator.querySelector('.pull-refresh-label');
    if (labelEl) labelEl.textContent = (typeof t === 'function') ? t('refreshing') : 'Refreshing…';
    _pullRefreshSetTransform(indicator, PULL_REFRESH_THRESHOLD);
    Promise.resolve()
      .then(() => (typeof refreshSession === 'function') ? refreshSession() : null)
      .catch(() => {})
      .finally(() => {
        // Hold the spinner briefly so the user perceives the refresh
        // before we slide it back up.
        setTimeout(() => _pullRefreshReset(indicator, true), 260);
      });
  };
  messages.addEventListener('touchend', onEnd, { passive: true });
  messages.addEventListener('touchcancel', onEnd, { passive: true });
}

function toggleDashboardMobileRail(force) {
  // Slide the dashboard-right column (hero / KPIs / quick actions) in and
  // out on mobile (<=760px CSS breakpoint). On desktop the rail is always
  // visible, so the toggle is a no-op above that width.
  const rail = document.querySelector('main.main.showing-dashboard .dashboard-right');
  const overlay = document.getElementById('dashboardMobileOverlay');
  const btn = document.getElementById('btnDashboardMobileRail');
  if (!rail) return;
  const willOpen = (typeof force === 'boolean')
    ? force
    : !rail.classList.contains('mobile-open');
  rail.classList.toggle('mobile-open', willOpen);
  if (overlay) overlay.classList.toggle('visible', willOpen);
  if (btn) btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  if (willOpen) {
    // Esc closes the drawer; bind once per open cycle.
    const onKey = (e) => {
      if (e.key === 'Escape') {
        toggleDashboardMobileRail(false);
        document.removeEventListener('keydown', onKey, true);
      }
    };
    document.addEventListener('keydown', onKey, true);
  }
}

function mountDashboardChat() {
  const messages = document.getElementById('messages');
  const composer = document.getElementById('composerWrap');
  const messageSlot = document.getElementById('dashboardChatMessagesSlot');
  const composerSlot = document.getElementById('dashboardChatComposerSlot');
  if (!messages || !composer || !messageSlot || !composerSlot) return;

  if (!chatMessagesAnchor && messages.parentNode) {
    chatMessagesAnchor = document.createComment('chatMessagesAnchor');
    messages.parentNode.insertBefore(chatMessagesAnchor, messages);
  }
  if (!chatComposerAnchor && composer.parentNode) {
    chatComposerAnchor = document.createComment('chatComposerAnchor');
    composer.parentNode.insertBefore(chatComposerAnchor, composer);
  }

  if (messages.parentNode !== messageSlot) messageSlot.appendChild(messages);
  if (composer.parentNode !== composerSlot) composerSlot.appendChild(composer);
}

function restoreDashboardChat() {
  const messages = document.getElementById('messages');
  const composer = document.getElementById('composerWrap');
  const messageSlot = document.getElementById('dashboardChatMessagesSlot');
  const composerSlot = document.getElementById('dashboardChatComposerSlot');

  if (messages && messages.parentNode === messageSlot) _insertAfter(chatMessagesAnchor, messages);
  if (composer && composer.parentNode === composerSlot) _insertAfter(chatComposerAnchor, composer);
}

let settingsMenuAnchor = null;

function mountDashboardSettings() {
  const menu = document.getElementById('settingsMenu');
  const mainSettings = document.getElementById('mainSettings');
  if (!menu || !mainSettings) return;
  if (!settingsMenuAnchor && menu.parentNode) {
    settingsMenuAnchor = document.createComment('settingsMenuAnchor');
    menu.parentNode.insertBefore(settingsMenuAnchor, menu);
  }
  if (menu.parentNode !== mainSettings) mainSettings.insertBefore(menu, mainSettings.firstChild);
}

function restoreDashboardSettings() {
  const menu = document.getElementById('settingsMenu');
  const mainSettings = document.getElementById('mainSettings');
  if (!menu || !mainSettings || menu.parentNode !== mainSettings) return;
  if (settingsMenuAnchor) _insertAfter(settingsMenuAnchor, menu);
}

let chatListPanelAnchor = null;

function mountDashboardChatList() {
  // Mirror of mountDashboardSkills: when the user opens the Conversas tab,
  // surface #panelChat (the historical session list) as a 260px sidecar
  // inside #mainChat — keeping the Neo dashboard chrome (left rail menu,
  // topbar, hero) intact instead of falling back to the legacy hermes shell.
  const panel = document.getElementById('panelChat');
  const mainChat = document.getElementById('mainChat');
  if (!panel || !mainChat) return;
  if (!chatListPanelAnchor && panel.parentNode) {
    chatListPanelAnchor = document.createComment('chatListPanelAnchor');
    panel.parentNode.insertBefore(chatListPanelAnchor, panel);
  }
  if (panel.parentNode !== mainChat) mainChat.insertBefore(panel, mainChat.firstChild);
}

function restoreDashboardChatList() {
  const panel = document.getElementById('panelChat');
  const mainChat = document.getElementById('mainChat');
  if (!panel || !mainChat || panel.parentNode !== mainChat) return;
  if (chatListPanelAnchor) _insertAfter(chatListPanelAnchor, panel);
}

let skillsPanelAnchor = null;

function mountDashboardSkills() {
  const panel = document.getElementById('panelSkills');
  const mainSkills = document.getElementById('mainSkills');
  if (!panel || !mainSkills) return;
  if (!skillsPanelAnchor && panel.parentNode) {
    skillsPanelAnchor = document.createComment('skillsPanelAnchor');
    panel.parentNode.insertBefore(skillsPanelAnchor, panel);
  }
  if (panel.parentNode !== mainSkills) mainSkills.insertBefore(panel, mainSkills.firstChild);
}

function restoreDashboardSkills() {
  const panel = document.getElementById('panelSkills');
  const mainSkills = document.getElementById('mainSkills');
  if (!panel || !mainSkills || panel.parentNode !== mainSkills) return;
  if (skillsPanelAnchor) _insertAfter(skillsPanelAnchor, panel);
}

let tasksPanelAnchor = null;

function mountDashboardTasks() {
  const panel = document.getElementById('panelTasks');
  const mainTasks = document.getElementById('mainTasks');
  if (!panel || !mainTasks) return;
  if (!tasksPanelAnchor && panel.parentNode) {
    tasksPanelAnchor = document.createComment('tasksPanelAnchor');
    panel.parentNode.insertBefore(tasksPanelAnchor, panel);
  }
  if (panel.parentNode !== mainTasks) mainTasks.insertBefore(panel, mainTasks.firstChild);
}

function restoreDashboardTasks() {
  const panel = document.getElementById('panelTasks');
  const mainTasks = document.getElementById('mainTasks');
  if (!panel || !mainTasks || panel.parentNode !== mainTasks) return;
  if (tasksPanelAnchor) _insertAfter(tasksPanelAnchor, panel);
}

async function loadDashboardSummary() {
  try {
    const data = await api('/api/dashboard/summary');
    renderDashboardKpis(data && data.cards);
  } catch (_) {
    renderDashboardKpis(DASHBOARD_FALLBACK_KPIS);
  }
}

async function focusDashboardComposer() {
  if (typeof switchPanel === 'function') {
    const switched = await switchPanel('dashboard');
    if (switched === false) return;
  }
  setTimeout(() => {
    const input = document.getElementById('msg');
    if (input) input.focus();
  }, 0);
}

function handleDashboardQuickAction(action) {
  if (action === 'open_terminal') {
    focusDashboardComposer();
    setTimeout(() => {
      if (typeof toggleComposerTerminal === 'function') toggleComposerTerminal(true);
      else if (typeof showToast === 'function') showToast(_t('terminal_no_workspace_title'), 2600, 'warning');
    }, 0);
    return;
  }

  const prompt = DASHBOARD_QUICK_ACTION_PROMPTS[action];
  focusDashboardComposer();
  setTimeout(() => {
    const input = document.getElementById('msg');
    if (input && prompt && !input.value.trim()) {
      input.value = prompt;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
    if (typeof showToast === 'function') showToast(_t('dashboard_action_placeholder'), 2600, 'info');
  }, 0);
}

function openComposerTerminal() {
  handleDashboardQuickAction('open_terminal');
}

function _dashboardEsc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function _dashboardTopbarPanel() {
  let panel = document.getElementById('dashboardTopbarPanel');
  if (panel) return panel;
  panel = document.createElement('div');
  panel.id = 'dashboardTopbarPanel';
  panel.className = 'dashboard-topbar-panel';
  panel.hidden = true;
  const actions = document.querySelector('.dashboard-topbar-actions');
  if (actions) actions.appendChild(panel);
  return panel;
}

function closeDashboardTopbarPanel() {
  const panel = document.getElementById('dashboardTopbarPanel');
  if (panel) panel.hidden = true;
}

function _renderDashboardTopbarPanel(kind, title, bodyHtml) {
  const panel = _dashboardTopbarPanel();
  panel.dataset.panelKind = kind;
  panel.hidden = false;
  panel.innerHTML = `
    <div class="dashboard-topbar-panel-head">
      <strong>${_dashboardEsc(title)}</strong>
      <button type="button" aria-label="Close" onclick="closeDashboardTopbarPanel()">x</button>
    </div>
    <div class="dashboard-topbar-panel-body">${bodyHtml}</div>
  `;
  return panel;
}

async function _runDashboardSearch(query) {
  const resultsEl = document.getElementById('dashboardSearchResults');
  if (!resultsEl) return;
  const q = String(query || '').trim();
  if (!q) {
    resultsEl.innerHTML = `<div class="dashboard-panel-empty">${_dashboardEsc(_t('dashboard_search_hint'))}</div>`;
    return;
  }
  resultsEl.innerHTML = `<div class="dashboard-panel-empty">${_dashboardEsc(_t('loading'))}</div>`;
  try {
    const data = await api(`/api/sessions/search?q=${encodeURIComponent(q)}&content=1&depth=8`);
    const sessions = Array.isArray(data && data.sessions) ? data.sessions.slice(0, 8) : [];
    if (!sessions.length) {
      resultsEl.innerHTML = `<div class="dashboard-panel-empty">${_dashboardEsc(_t('dashboard_search_no_results'))}</div>`;
      return;
    }
    resultsEl.innerHTML = '';
    sessions.forEach(session => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dashboard-search-result';
      btn.innerHTML = `<strong>${_dashboardEsc(session.title || 'Untitled')}</strong><span>${_dashboardEsc(session.match_type || 'title')}</span>`;
      btn.addEventListener('click', async () => {
        if (typeof loadSession === 'function') await loadSession(session.session_id);
        closeDashboardTopbarPanel();
        focusDashboardComposer();
      });
      resultsEl.appendChild(btn);
    });
  } catch (err) {
    resultsEl.innerHTML = `<div class="dashboard-panel-empty">${_dashboardEsc((err && err.message) || _t('failed_colon'))}</div>`;
  }
}

function openDashboardSearch() {
  toggleDashboardAdminMenu(false);
  const panel = _renderDashboardTopbarPanel('search', _t('topbar_search'), `
    <input class="dashboard-search-input" id="dashboardSearchInput" type="search" autocomplete="off" placeholder="${_dashboardEsc(_t('filter_conversations'))}">
    <div class="dashboard-search-results" id="dashboardSearchResults"></div>
  `);
  const input = panel.querySelector('#dashboardSearchInput');
  if (input) {
    input.addEventListener('input', () => {
      clearTimeout(dashboardSearchTimer);
      dashboardSearchTimer = setTimeout(() => _runDashboardSearch(input.value), 250);
    });
    setTimeout(() => input.focus(), 0);
  }
  _runDashboardSearch('');
}

async function openDashboardNotifications() {
  toggleDashboardAdminMenu(false);
  const supported = 'Notification' in window;
  let permission = supported ? Notification.permission : 'unsupported';
  let enabled = !!window._notificationsEnabled;
  if (supported && permission === 'default') {
    permission = await Notification.requestPermission();
  }
  if (supported && permission === 'granted') {
    enabled = true;
    window._notificationsEnabled = true;
    const checkbox = document.getElementById('settingsNotificationsEnabled');
    if (checkbox) checkbox.checked = true;
    try {
      await api('/api/settings', { method: 'POST', body: JSON.stringify({ notifications_enabled: true }) });
    } catch (_) {}
  }
  const statusKey = !supported ? 'dashboard_notifications_unsupported'
    : permission === 'granted' && enabled ? 'dashboard_notifications_enabled'
    : permission === 'denied' ? 'dashboard_notifications_blocked'
    : 'dashboard_notifications_disabled';
  const panel = _renderDashboardTopbarPanel('notifications', _t('topbar_notifications'), `
    <div class="dashboard-notification-status">${_dashboardEsc(_t(statusKey))}</div>
    <button class="dashboard-panel-action" id="dashboardNotificationSettings" type="button">${_dashboardEsc(_t('tab_settings'))}</button>
  `);
  const settingsBtn = panel.querySelector('#dashboardNotificationSettings');
  if (settingsBtn) {
    settingsBtn.addEventListener('click', async () => {
      closeDashboardTopbarPanel();
      if (typeof switchPanel === 'function') await switchPanel('settings');
      if (typeof switchSettingsSection === 'function') switchSettingsSection('preferences');
    });
  }
}

function openDashboardHelp() {
  toggleDashboardAdminMenu(false);
  const panel = _renderDashboardTopbarPanel('help', _t('topbar_help'), `
    <div class="dashboard-help-actions">
      <button class="dashboard-panel-action" id="dashboardHelpCommands" type="button">${_dashboardEsc(_t('available_commands'))}</button>
      <button class="dashboard-panel-action" id="dashboardHelpSettings" type="button">${_dashboardEsc(_t('tab_settings'))}</button>
      <button class="dashboard-panel-action" id="dashboardHelpChat" type="button">${_dashboardEsc(_t('tab_chat'))}</button>
    </div>
  `);
  const commandsBtn = panel.querySelector('#dashboardHelpCommands');
  if (commandsBtn) {
    commandsBtn.addEventListener('click', () => {
      closeDashboardTopbarPanel();
      if (typeof cmdHelp === 'function') cmdHelp();
      focusDashboardComposer();
    });
  }
  const settingsBtn = panel.querySelector('#dashboardHelpSettings');
  if (settingsBtn) {
    settingsBtn.addEventListener('click', async () => {
      closeDashboardTopbarPanel();
      if (typeof switchPanel === 'function') await switchPanel('settings');
    });
  }
  const chatBtn = panel.querySelector('#dashboardHelpChat');
  if (chatBtn) {
    chatBtn.addEventListener('click', () => {
      closeDashboardTopbarPanel();
      focusDashboardComposer();
    });
  }
}

function toggleDashboardAdminMenu(open) {
  const btn = document.getElementById('dashboardAdminBtn');
  const menu = document.getElementById('dashboardAdminMenu');
  if (!btn || !menu) return;
  const next = typeof open === 'boolean' ? open : menu.hidden;
  menu.hidden = !next;
  btn.setAttribute('aria-expanded', next ? 'true' : 'false');
}

function handleDashboardAdminMenu(action) {
  toggleDashboardAdminMenu(false);
  if (action === 'profiles') {
    if (typeof switchPanel === 'function') switchPanel('profiles');
    return;
  }
  if (action === 'settings') {
    if (typeof switchPanel === 'function') switchPanel('settings');
    setTimeout(() => {
      if (typeof switchSettingsSection === 'function') switchSettingsSection('preferences');
    }, 0);
    return;
  }
  if (action === 'logout' && typeof signOut === 'function') signOut();
}

function handleDashboardTopbarAction(action) {
  if (action === 'admin') {
    closeDashboardTopbarPanel();
    toggleDashboardAdminMenu();
    return;
  }
  toggleDashboardAdminMenu(false);
  if (action === 'search') {
    openDashboardSearch();
    return;
  }
  if (action === 'notifications') {
    openDashboardNotifications();
    return;
  }
  if (action === 'help') {
    openDashboardHelp();
  }
}

function bindDashboardAdminMenu() {
  if (window.__neoDashboardAdminBound) return;
  window.__neoDashboardAdminBound = true;
  document.addEventListener('click', event => {
    const menu = document.getElementById('dashboardAdminMenu');
    const btn = document.getElementById('dashboardAdminBtn');
    const panel = document.getElementById('dashboardTopbarPanel');
    if (panel && !panel.hidden && panel.contains(event.target)) return;
    if (panel && !panel.hidden && !event.target.closest('.dashboard-topbar-icon')) closeDashboardTopbarPanel();
    if (!menu || !btn || menu.hidden) return;
    if (menu.contains(event.target) || btn.contains(event.target)) return;
    toggleDashboardAdminMenu(false);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      toggleDashboardAdminMenu(false);
      closeDashboardTopbarPanel();
    }
  });
}

function renderNeoPersonalPanel() {
  const root = document.getElementById('neoPersonalOverview');
  if (!root) return;
  const activeProfile = (typeof S === 'object' && S && S.activeProfile) || 'default';
  const language = localStorage.getItem('hermes-lang') || 'pt-BR';
  const theme = localStorage.getItem('hermes-theme') || 'dark';
  const skin = localStorage.getItem('hermes-skin') || 'neo';
  const defaultPanelEl = document.getElementById('settingsDefaultPanel');
  const defaultPanel = defaultPanelEl && defaultPanelEl.value ? defaultPanelEl.value : 'dashboard';
  const profileEl = document.getElementById('neoPersonalProfileName');
  const languageEl = document.getElementById('neoPersonalLanguage');
  const defaultPanelTextEl = document.getElementById('neoPersonalDefaultPanel');
  const themeSkinEl = document.getElementById('neoPersonalThemeSkin');
  if (profileEl) profileEl.textContent = activeProfile;
  if (languageEl) languageEl.textContent = language;
  if (defaultPanelTextEl) defaultPanelTextEl.textContent = defaultPanel === 'dashboard' ? 'Dashboard' : 'Chat';
  if (themeSkinEl) themeSkinEl.textContent = `${theme} / ${skin}`;
}

async function openNeoPersonalSettings() {
  if (typeof switchPanel === 'function') await switchPanel('settings');
  if (typeof switchSettingsSection === 'function') switchSettingsSection('preferences');
}

function renderDashboardSystemHealth(data) {
  const status = document.getElementById('dashboardSystemStatus');
  const uptime = document.getElementById('dashboardSystemUptime');
  const region = document.getElementById('dashboardSystemRegion');
  const version = document.getElementById('dashboardSystemVersion');
  if (status) status.textContent = data && data.user || 'Root';
  if (uptime) uptime.textContent = data && data.uptime || '--';
  if (region) region.textContent = data && data.region || '--';
  if (version) version.textContent = data && data.version || '--';
}

function renderDashboardVpsHealth(data) {
  const metrics = data && Array.isArray(data.metrics) ? data.metrics : [];
  metrics.forEach(metric => {
    const row = document.querySelector(`[data-vps-metric="${metric.id}"]`);
    if (!row) return;
    const value = Math.max(0, Math.min(100, Number(metric.value || 0)));
    const label = row.querySelector('b');
    const bar = row.querySelector('.neo-vps-bar i');
    if (label) label.textContent = `${Math.round(value)}%`;
    if (bar) bar.style.width = `${value}%`;
  });
}

async function loadDashboardHealth(force = false) {
  const now = Date.now();
  if (!force && dashboardHealthCache.at && now - dashboardHealthCache.at < 5000) {
    if (dashboardHealthCache.system) renderDashboardSystemHealth(dashboardHealthCache.system);
    if (dashboardHealthCache.vps) renderDashboardVpsHealth(dashboardHealthCache.vps);
    return;
  }
  try {
    const system = await api('/api/health/system');
    dashboardHealthCache.system = system;
    renderDashboardSystemHealth(system);
  } catch (_) {}
  try {
    const vps = await api('/api/health/vps');
    dashboardHealthCache.vps = vps;
    renderDashboardVpsHealth(vps);
  } catch (_) {}
  dashboardHealthCache.at = Date.now();
}

function startDashboardHealthPolling() {
  if (dashboardHealthPollTimer) return;
  dashboardHealthPollTimer = setInterval(() => {
    if (!document.hidden) loadDashboardHealth(true);
  }, 30000);
}

async function loadDashboard() {
  const root = document.getElementById('mainDashboard');
  if (!root) return;

  const now = new Date();
  const stamp = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  document.querySelectorAll('[data-dashboard-updated-at]').forEach(el => { el.textContent = stamp; });

  const greetingEl = document.getElementById('heroGreetingTime');
  if (greetingEl) greetingEl.textContent = _t(_getGreetingKey());

  mountDashboardChat();
  await loadDashboardSummary();
  await loadDashboardHealth();
  startDashboardHealthPolling();
  bindDashboardAdminMenu();

  if (_dashboardLoaded) return;
  _dashboardLoaded = true;
}

function mountDashboardAgents() {
  var frame = document.getElementById('agentsAppFrame');
  if (!frame) return;
  if (!frame.src || frame.src === 'about:blank' || frame.src === '') {
    frame.src = '/static/agents-app/index-neo.html';
  }
  frame.style.display = 'block';
  var empty = document.getElementById('agentsEmptyState');
  if (empty) empty.style.display = 'none';
}

function restoreDashboardAgents() {
  var frame = document.getElementById('agentsAppFrame');
  if (!frame) return;
  frame.style.display = 'none';
  var empty = document.getElementById('agentsEmptyState');
  if (empty) empty.style.display = '';
}

document.addEventListener('DOMContentLoaded', _initPullToRefresh);
