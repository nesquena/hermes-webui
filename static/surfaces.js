// hermes-webui -- Surfaces dashboard stub.
// Full implementation lands in section 7 of
// openspec/changes/add-dashboards-and-pixel-office/tasks.md.
//
// Stage 2 deliverable: avoid runtime error when the Surfaces tab is
// clicked — render a friendly placeholder fed by /api/surfaces, no SSE yet.

(function() {
  'use strict';

  window.showSurfaces = async function() {
    const grid = document.getElementById('surfacesCardGrid');
    if (!grid) return;
    grid.innerHTML = '<div class="insights-empty">Loading...</div>';
    try {
      const data = await window.api('/api/surfaces');
      const surfaces = (data && data.surfaces) || [];
      if (!surfaces.length) {
        grid.innerHTML = `<div class="insights-empty">${(window.t && window.t('insights_empty')) || 'No activity yet.'}</div>`;
        return;
      }
      grid.innerHTML = surfaces.map(_renderCard).join('');
    } catch (e) {
      grid.innerHTML = `<div class="insights-empty">Error: ${String(e && e.message || e)}</div>`;
    }
  };

  window.hideSurfaces = function() {
    // Section 7 will close the SSE connection here.
  };

  const ICONS = {
    cli:       '💻', webui:    '🌐', weixin:   '💬', telegram: '✈️',
    discord:   '🎮', slack:    '#',  signal:   '📡', whatsapp: '💚',
    sms:       '✉️', email:    '📧', cron:     '⏱',
  };

  function _renderCard(s) {
    const esc = window.esc || (x => String(x));
    const icon = ICONS[s.source] || '📦';
    const state = s.state || 'offline';
    const last = s.last_active_ts ? _rel(s.last_active_ts) : '—';
    const msgs = s.message_count_24h || 0;
    const tokens = s.tokens_24h || 0;
    const webuiLine = (s.source === 'webui' && typeof s.active_webui_sessions !== 'undefined')
      ? `<div class="surface-card-row">${s.active_webui_sessions} active webui sessions</div>`
      : '';
    return `
      <div class="surface-card" data-source="${esc(s.source)}">
        <div class="surface-card-head">
          <span class="surface-card-icon">${icon}</span>
          <span class="surface-card-name">${esc(s.source)}</span>
          <span class="surface-state-light ${esc(state)}" title="${esc(state)}"></span>
        </div>
        <div class="surface-card-row">Last message ${esc(last)}</div>
        <div class="surface-card-row">${msgs} messages · ${tokens} tokens (24h)</div>
        ${webuiLine}
      </div>`;
  }

  function _rel(ts) {
    const dt = Date.now() / 1000 - ts;
    if (dt < 60) return Math.round(dt) + 's ago';
    if (dt < 3600) return Math.round(dt / 60) + 'm ago';
    if (dt < 86400) return Math.round(dt / 3600) + 'h ago';
    return Math.round(dt / 86400) + 'd ago';
  }
})();
