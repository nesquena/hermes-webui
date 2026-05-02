let _dashboardLoaded = false;
let chatMessagesAnchor = null;
let chatComposerAnchor = null;
let dashboardHealthPollTimer = null;
let dashboardHealthCache = { at: 0, system: null, vps: null };

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

async function loadDashboardSummary() {
  try {
    const data = await api('/api/dashboard/summary');
    renderDashboardKpis(data && data.cards);
  } catch (_) {
    renderDashboardKpis(DASHBOARD_FALLBACK_KPIS);
  }
}

function focusDashboardComposer() {
  if (typeof switchPanel === 'function') switchPanel('dashboard');
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

function handleDashboardTopbarAction(action) {
  if (action === 'admin') {
    if (typeof switchPanel === 'function') switchPanel('settings');
    return;
  }
  if (typeof showToast === 'function') showToast(_t('dashboard_topbar_placeholder'), 2400, 'info');
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

  if (_dashboardLoaded) return;
  _dashboardLoaded = true;
}
