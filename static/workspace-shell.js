/* Role-aware workspace presentation. Effective API permissions remain server-owned.
   Additional navigation is a personal disclosure; it never reveals denied tabs. */
(function () {
  'use strict';
  const kinds = new Set(['member', 'operator', 'engineer', 'admin']);
  function text(key) { return typeof t === 'function' ? t(key) : key; }
  function preferenceKey() {
    return 'synthpulse-workspace-more:' + String((window.__GOV_ME__ || {}).email || 'local');
  }
  const entry = new URL(location.href);
  let pendingHome = entry.pathname === new URL(document.baseURI).pathname && !entry.search && !entry.hash && window === window.top;
  function allowed(panel) {
    const nav = document.querySelector('.rail .nav-tab[data-panel="' + panel + '"]');
    return Array.isArray(window.__GOV_ME__?.hidden_nav) && nav && !nav.hidden && !nav.disabled &&
      !nav.classList.contains('nav-tab-hidden') && !window.__GOV_ME__.hidden_nav.includes(panel) &&
      (!window.SynthPulseModules || window.SynthPulseModules.canOpen(panel));
  }
  function startKey() { return 'synthpulse-start:' + String(window.__GOV_ME__?.email || 'local'); }
  function startChoice() {
    try { return localStorage.getItem(startKey()) === 'chat' ? 'chat' : 'overview'; } catch (_) { return 'overview'; }
  }
  function applyStart() {
    if (!pendingHome || typeof S === 'undefined' || !S._bootReady || !Array.isArray(window._govHiddenNav)) return;
    pendingHome = false;
    const draft = S.session?.composer_draft;
    if (!allowed('studio') || startChoice() === 'chat' || _currentPanel !== 'chat' || S.busy || S.activeStreamId ||
        S.session?.active_stream_id || S.session?.pending_user_message || draft?.text || draft?.files?.length ||
        document.getElementById('msg')?.value.trim() || window.__HERMES_PANE_MODE) return;
    switchPanel('studio');
  }
  function expanded() {
    try { return localStorage.getItem(preferenceKey()) === '1'; } catch (_) { return false; }
  }
  function toggle() {
    const next = !expanded();
    try { localStorage.setItem(preferenceKey(), next ? '1' : '0'); } catch (_) { /* storage unavailable */ }
    sync(next);
  }
  function sync(forcedExpanded) {
    const me = window.__GOV_ME__ || {};
    const view = me.workspace_navigation || {};
    const kind = kinds.has(view.kind) ? view.kind : me.nav_audience === 'admin' ? 'admin' : 'member';
    document.documentElement.dataset.workspaceKind = kind;
    const secondary = new Set(Array.isArray(view.secondary) ? view.secondary : []);
    const more = typeof forcedExpanded === 'boolean' ? forcedExpanded : expanded();
    const active = document.querySelector('.rail .nav-tab.active[data-panel]');
    let hasAdditional = false;
    document.querySelectorAll('.rail .nav-tab[data-panel], .sidebar-nav .nav-tab[data-panel]').forEach(el => {
      const additional = kind !== 'admin' && secondary.has(el.dataset.panel);
      if (additional && !el.hidden && !el.classList.contains('nav-tab-hidden')) hasAdditional = true;
      // Keep an active secondary destination in reach when the disclosure closes.
      el.classList.toggle('workspace-secondary-hidden', additional && !more && el.dataset.panel !== active?.dataset.panel);
    });
    document.querySelectorAll('.workspace-more').forEach(button => {
      button.hidden = !hasAdditional;
      button.setAttribute('aria-expanded', String(more));
      button.setAttribute('aria-label', text(more ? 'workspace_less' : 'workspace_more'));
      button.dataset.tooltip = text(more ? 'workspace_less' : 'workspace_more');
      button.querySelector('span').textContent = text(more ? 'workspace_less' : 'workspace_more');
    });
    const badge = document.getElementById('workspaceRoleBadge');
    if (badge) {
      badge.textContent = text('workspace_kind_' + kind);
      badge.title = text('workspace_role_hint');
    }
    const field = document.getElementById('workspaceStartField');
    if (field) {
      field.hidden = !allowed('studio');
      document.getElementById('settingsStartPage').value = startChoice();
    }
    const home = document.getElementById('workspaceHome');
    if (home) home.disabled = !allowed('studio');
    applyStart();
    if (window.SPNavGroups) window.SPNavGroups.sync();
  }
  function start() {
    for (const selector of ['.rail', '.sidebar-nav']) {
      const nav = document.querySelector(selector);
      if (!nav) continue;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'workspace-more';
      button.hidden = true;
      button.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/></svg><span></span>';
      button.addEventListener('click', toggle);
      nav.insertBefore(button, nav.querySelector('.rail-spacer, [data-panel="settings"]'));
    }
    sync();
    document.addEventListener('click', event => {
      if (event.target.closest('.nav-tab[data-panel]')) requestAnimationFrame(() => sync());
    });
    new MutationObserver(() => sync()).observe(document.documentElement, {attributes: true, attributeFilter: ['lang']});
  }
  document.addEventListener('pointerdown', () => { pendingHome = false; }, {once:true, capture:true});
  document.addEventListener('keydown', () => { pendingHome = false; }, {once:true, capture:true});
  document.getElementById('settingsStartPage')?.addEventListener('change', event => {
    try { localStorage.setItem(startKey(), event.target.value); } catch (_) { showToast(text('workspace_start_failed')); }
  });
  document.getElementById('workspaceHome')?.addEventListener('click', async () => {
    if (!allowed('studio')) return;
    if (_currentPanel !== 'studio' && !(await switchPanel('studio'))) return;
    if (_currentPanel === 'studio') { window.SPStudio.setTab('home'); closeMobileSidebar(); }
  });
  window.addEventListener('synpulse:boot-ready', applyStart);
  window.SynthPulseWorkspace = {sync, allowed};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
