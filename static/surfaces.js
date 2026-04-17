// hermes-webui -- Surfaces dashboard.
// Section 7 of openspec/changes/add-dashboards-and-pixel-office/tasks.md.
//
// Flow:
//   showSurfaces() → initial GET /api/surfaces + open SSE stream
//   SSE event: snapshot     → replace full card set
//   SSE event: delta        → patch the subset that changed
//   SSE event: heartbeat    → toggle connection light, no-op
//   SSE event: profile_changed → close socket; reconnect fetches new profile
//   Click a card → GET /api/surfaces?source=X&expand=1 and render
//                  a drawer with up to 5 recent sessions. Click again → close.
//   Click another tab → hideSurfaces() tears down the EventSource.

(function() {
  'use strict';

  const ICONS = {
    cli:       '💻', webui:    '🌐', weixin:   '💬', telegram: '✈️',
    discord:   '🎮', slack:    '#',  signal:   '📡', whatsapp: '💚',
    sms:       '✉️', email:    '📧', cron:     '⏱',
  };
  const STATES = new Set(['working', 'waiting', 'idle', 'offline']);
  const EXPAND_CACHE_TTL_MS = 30 * 1000;

  const state = {
    surfaces: new Map(),       // source -> surface record
    expanded: new Map(),       // source -> { opened_at_ms, sessions }
    openSource: null,          // currently expanded card's source, if any
    sse: null,                 // EventSource
    reconnectTimer: null,
    reconnectDelayMs: 5000,
  };

  window.showSurfaces = async function() {
    await _initialLoad();
    _openStream();
  };

  window.hideSurfaces = function() {
    _closeStream();
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  };

  // ── Initial fetch (non-SSE) ──
  async function _initialLoad() {
    const grid = _grid();
    if (!grid) return;
    grid.innerHTML = `<div class="insights-empty">${_t('loading')}</div>`;
    try {
      const data = await window.api('/api/surfaces');
      _applySnapshot(data);
    } catch (e) {
      grid.innerHTML = `<div class="insights-empty">${_esc('Error: ' + (e && e.message || e))}</div>`;
    }
  }

  // ── SSE wiring ──
  function _openStream() {
    _closeStream();
    try {
      state.sse = new EventSource('api/agent-activity/stream');
    } catch (e) { _setConnLight('offline'); return _scheduleReconnect(); }
    _setConnLight('connecting');
    state.sse.addEventListener('snapshot', _onSnapshot);
    state.sse.addEventListener('delta', _onDelta);
    state.sse.addEventListener('heartbeat', _onHeartbeat);
    state.sse.addEventListener('profile_changed', _onProfileChanged);
    state.sse.addEventListener('error', _onError);
    state.sse.addEventListener('open', () => _setConnLight('working'));
  }

  function _closeStream() {
    if (state.sse) {
      try { state.sse.close(); } catch (_) {}
      state.sse = null;
    }
    _setConnLight('offline');
  }

  function _scheduleReconnect() {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = setTimeout(() => {
      if (document.querySelector('.nav-tab[data-panel="surfaces"].active')) {
        _openStream();
      }
    }, state.reconnectDelayMs);
  }

  function _onSnapshot(ev) {
    try { _applySnapshot(JSON.parse(ev.data)); } catch (_) {}
  }

  function _onDelta(ev) {
    try {
      const delta = JSON.parse(ev.data);
      _applyDelta(delta);
    } catch (_) {}
  }

  function _onHeartbeat() {
    _setConnLight('working');
  }

  function _onProfileChanged() {
    // Server will close connection; reset our snapshot so the reconnect sees fresh surfaces.
    state.surfaces.clear();
    _closeStream();
    // Immediate refetch for the new profile
    _initialLoad().then(_openStream);
  }

  function _onError() {
    _setConnLight('offline');
    _closeStream();
    _scheduleReconnect();
  }

  // ── Snapshot / delta application ──
  function _applySnapshot(data) {
    state.surfaces.clear();
    const surfaces = (data && data.surfaces) || [];
    for (const s of surfaces) state.surfaces.set(s.source, s);
    _renderAll();
  }

  function _applyDelta(delta) {
    const changes = (delta && delta.surfaces) || [];
    for (const s of changes) {
      if (s.state === 'offline' && Object.keys(s).length <= 3) {
        // Server's "removed" marker — keep the card but mark offline
        const prev = state.surfaces.get(s.source);
        state.surfaces.set(s.source, { ...(prev || {}), ...s });
      } else {
        state.surfaces.set(s.source, s);
      }
    }
    _renderAll();
  }

  // ── Rendering ──
  function _renderAll() {
    const grid = _grid();
    if (!grid) return;
    if (!state.surfaces.size) {
      grid.innerHTML = `<div class="insights-empty">${_t('insights_empty')}</div>`;
      _renderSidebarSummary();
      return;
    }
    const entries = Array.from(state.surfaces.values()).sort((a, b) => (b.last_active_ts || 0) - (a.last_active_ts || 0));
    grid.innerHTML = entries.map(_renderCard).join('');
    _attachCardHandlers();
    _renderSidebarSummary();
    // Re-open the previously expanded drawer after re-render
    if (state.openSource) _renderExpandInline(state.openSource);
  }

  function _renderSidebarSummary() {
    const el = document.getElementById('surfacesSidebar');
    if (!el) return;
    if (!state.surfaces.size) { el.textContent = '—'; return; }
    const counts = { working: 0, waiting: 0, idle: 0, offline: 0 };
    for (const s of state.surfaces.values()) {
      const st = STATES.has(s.state) ? s.state : 'offline';
      counts[st] += 1;
    }
    el.innerHTML = `<span style="color:#4ade80">${counts.working}</span>
      · <span style="color:#fbbf24">${counts.waiting}</span>
      · <span style="color:#9ca3af">${counts.idle}</span>
      · <span style="color:#6b7280">${counts.offline}</span>`;
  }

  function _renderCard(s) {
    const icon = ICONS[s.source] || '📦';
    const stateName = STATES.has(s.state) ? s.state : 'offline';
    const last = s.last_active_ts ? _rel(s.last_active_ts) : '—';
    const msgs = s.message_count_24h || 0;
    const tokens = s.tokens_24h || 0;
    const webuiLine = (s.source === 'webui' && typeof s.active_webui_sessions !== 'undefined')
      ? `<div class="surface-card-row">${s.active_webui_sessions} active webui sessions</div>`
      : '';
    const expandSlot = (state.openSource === s.source)
      ? `<div class="surface-card-expand" id="expand-${_esc(s.source)}"><div class="insights-empty" style="padding:10px 0">${_t('loading')}</div></div>`
      : '';
    return `
      <div class="surface-card" data-source="${_esc(s.source)}">
        <div class="surface-card-head">
          <span class="surface-card-icon">${icon}</span>
          <span class="surface-card-name">${_esc(s.source)}</span>
          <span class="surface-state-light ${_esc(stateName)}" title="state: ${_esc(stateName)}"></span>
        </div>
        <div class="surface-card-row">Last message ${_esc(last)}</div>
        <div class="surface-card-row">${msgs} messages · ${tokens} tokens (24h)</div>
        ${webuiLine}
        ${expandSlot}
      </div>`;
  }

  function _attachCardHandlers() {
    document.querySelectorAll('#surfacesCardGrid .surface-card').forEach(card => {
      card.addEventListener('click', () => _toggleExpand(card.dataset.source));
    });
  }

  // ── Expand drawer ──
  function _toggleExpand(source) {
    if (state.openSource === source) {
      state.openSource = null;
      _renderAll();
      return;
    }
    state.openSource = source;
    _renderAll();
    _renderExpandInline(source);
  }

  async function _renderExpandInline(source) {
    const slot = document.getElementById('expand-' + source);
    if (!slot) return;
    const cached = state.expanded.get(source);
    if (cached && (Date.now() - cached.opened_at_ms) < EXPAND_CACHE_TTL_MS) {
      _fillExpandSlot(slot, cached.sessions);
      return;
    }
    try {
      const data = await window.api(`/api/surfaces?source=${encodeURIComponent(source)}&expand=1`);
      const sessions = (data && data.sessions) || [];
      state.expanded.set(source, { opened_at_ms: Date.now(), sessions });
      _fillExpandSlot(slot, sessions);
    } catch (e) {
      slot.innerHTML = `<div class="insights-empty" style="padding:10px 0">${_esc('Error: ' + (e && e.message || e))}</div>`;
    }
  }

  function _fillExpandSlot(slot, sessions) {
    if (!sessions.length) {
      slot.innerHTML = `<div class="insights-empty" style="padding:10px 0">${_t('insights_empty')}</div>`;
      return;
    }
    slot.innerHTML = sessions.map(row => {
      const when = row.last_activity ? _rel(row.last_activity) : '—';
      const title = row.title || '(untitled)';
      const model = row.model || '—';
      const count = row.message_count || 0;
      return `
        <div class="surface-card-expand-item">
          <div class="surface-card-expand-title" title="${_esc(title)}">${_esc(title)}</div>
          <div class="surface-card-expand-meta">${_esc(model)} · ${count} · ${_esc(when)}</div>
        </div>`;
    }).join('');
  }

  // ── Helpers ──
  function _grid() { return document.getElementById('surfacesCardGrid'); }
  function _t(key) { return (window.t && window.t(key)) || key; }
  function _esc(s) { return (window.esc && window.esc(s)) || String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
  function _rel(ts) {
    const dt = Date.now() / 1000 - ts;
    if (dt < 60) return Math.round(dt) + 's ago';
    if (dt < 3600) return Math.round(dt / 60) + 'm ago';
    if (dt < 86400) return Math.round(dt / 3600) + 'h ago';
    return Math.round(dt / 86400) + 'd ago';
  }
  function _setConnLight(stateName) {
    const el = document.getElementById('surfacesConnLight');
    if (!el) return;
    const map = {
      working:   '#4ade80',
      connecting:'#fbbf24',
      offline:   '#4b5563',
    };
    el.style.background = map[stateName] || map.offline;
    el.title = `SSE ${stateName}`;
  }
})();
