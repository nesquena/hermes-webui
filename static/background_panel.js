let _bgPanelPollTimer = null;
let _bgPanelOpen = false;
let _bgPanelRows = { running: [], finished: [] };
const BG_PANEL_POLL_MS = 4000;

function _bgPanelSession() {
  try {
    return (typeof S !== 'undefined' && S && S.session) ? S.session.session_id : null;
  } catch (_) {
    return null;
  }
}

function _bgPanelFmtUptime(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function _bgPanelRowHtml(task) {
  const running = task.status === 'running';
  const statusText = running ? _bgPanelFmtUptime(task.uptime_seconds) : `finie${task.exit_code != null ? ' (' + task.exit_code + ')' : ''}`;
  const killBtn = running
    ? `<button class="bg-task-kill" data-process-id="${esc(task.process_id)}" title="Arrêter cette tâche">×</button>`
    : '';
  return `<div class="bg-task-row">
    <div class="bg-task-main">
      <div class="bg-task-cmd" title="${esc(task.command)}">${esc(task.command || task.process_id)}</div>
      <div class="bg-task-meta"><span class="bg-task-status ${running ? 'is-running' : 'is-done'}">${esc(statusText)}</span>
        <span class="bg-task-pid">pid ${esc(task.pid ?? '?')}</span></div>
    </div>
    ${killBtn}
  </div>`;
}

function _bgPanelRender() {
  const root = $('bgTaskPanelBody');
  if (!root) return;
  const running = _bgPanelRows.running || [];
  const finished = _bgPanelRows.finished || [];
  let html = `<div class="bg-task-section-title">En cours (${running.length})</div>`;
  html += running.length ? running.map(_bgPanelRowHtml).join('') : '<div class="bg-task-empty">Aucune tâche en cours</div>';
  html += `<div class="bg-task-section-title">Terminées (${finished.length})</div>`;
  html += finished.length ? finished.map(_bgPanelRowHtml).join('') : '<div class="bg-task-empty">Aucune tâche terminée</div>';
  root.innerHTML = html;
  root.querySelectorAll('.bg-task-kill').forEach(btn => {
    btn.addEventListener('click', () => _bgPanelKill(btn.dataset.processId));
  });
  _bgPanelSyncBadge(running.length);
}

function _bgPanelSyncBadge(count) {
  const badge = $('bgBadge');
  if (!badge) return;
  if (count > 0) {
    badge.textContent = String(count);
    badge.style.display = '';
  } else if (!_bgPanelOpen) {
    badge.style.display = 'none';
  }
}

async function _bgPanelKill(processId) {
  if (!processId) return;
  try {
    await api('/api/background/processes/kill', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ process_id: processId }),
    });
    if (typeof showToast === 'function') showToast('Tâche arrêtée', 2500, 'info');
  } catch (e) {
    if (typeof showToast === 'function') showToast("Impossible d'arrêter la tâche", 3500, 'warning');
  }
  await _bgPanelRefresh();
}

async function _bgPanelRefresh() {
  const sid = _bgPanelSession();
  if (!sid) return;
  try {
    const r = await api(`/api/background/processes?session_id=${encodeURIComponent(sid)}`, { retries: 0 });
    _bgPanelRows = { running: r.running || [], finished: r.finished || [] };
    if (_bgPanelOpen) _bgPanelRender();
    else _bgPanelSyncBadge((r.running || []).length);
  } catch (_) {}
}

function _bgPanelStartPolling() {
  if (_bgPanelPollTimer) return;
  _bgPanelRefresh();
  _bgPanelPollTimer = setInterval(_bgPanelRefresh, BG_PANEL_POLL_MS);
}

function toggleBgTaskPanel() {
  const panel = $('bgTaskPanel');
  if (!panel) return;
  _bgPanelOpen = !_bgPanelOpen;
  panel.style.display = _bgPanelOpen ? '' : 'none';
  if (_bgPanelOpen) _bgPanelRender();
  else _bgPanelSyncBadge((_bgPanelRows.running || []).length);
}

document.addEventListener('DOMContentLoaded', () => {
  const badge = $('bgBadge');
  if (badge) {
    badge.style.cursor = 'pointer';
    badge.addEventListener('click', toggleBgTaskPanel);
  }
  _bgPanelStartPolling();
});
