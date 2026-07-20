// ── Hermex/WebUI cron notification inbox ───────────────────────────────────
// Cron jobs can deliver to the profile-local WebUI inbox via deliver="webui".
// This client shows an unread badge and sidebar inbox for new SSE events.
// Completion toasts remain owned by the existing per-cron preference path so
// the inbox cannot emit a second alert for the same run. Reads aggregate
// visible profiles by default so Hermex can surface
// cron deliveries from role profiles like newsletteros while respecting the
// server's isolated-profile guard.

let _notificationsCache = [];
let _notificationsSelectedKey = '';
let _notificationsEventSource = null;
let _notificationsReconnectTimer = null;
let _notificationsSeenKeys = new Set();
let _notificationsUnreadCount = 0;

function _notificationsUrl(path, params) {
  const rel = String(path || '').replace(/^\//, '');
  const url = new URL(rel, document.baseURI || location.href);
  const p = params || {};
  Object.keys(p).forEach(k => {
    if (p[k] !== undefined && p[k] !== null && p[k] !== '') url.searchParams.set(k, String(p[k]));
  });
  return url;
}

function _notificationsApiParams(extra) {
  return Object.assign({ all_profiles: '1' }, extra || {});
}

function _notificationId(row) {
  return row && row.id != null ? String(row.id) : '';
}

function _notificationKey(row) {
  const id = _notificationId(row);
  if (!id) return '';
  return JSON.stringify([String(row && row.profile || ''), id]);
}

function _notificationCreated(row) {
  return row && row.created_at ? String(row.created_at) : '';
}

function _notificationTitle(row) {
  const title = row && (row.title || row.name || row.job_id);
  return String(title || 'Cron notification');
}

function _notificationBody(row) {
  return String((row && (row.body || row.content || row.text)) || '');
}

function _formatNotificationTime(value) {
  if (!value) return '';
  try {
    const date = new Date(value);
    if (!Number.isNaN(date.getTime())) return date.toLocaleString();
  } catch (_) {}
  return String(value);
}

function _sortNotifications(rows) {
  return (Array.isArray(rows) ? rows.slice() : []).sort((a, b) => _notificationCreated(b).localeCompare(_notificationCreated(a)));
}

function _mergeNotification(row) {
  const key = _notificationKey(row);
  if (!key) return false;
  const idx = _notificationsCache.findIndex(n => _notificationKey(n) === key);
  if (idx >= 0) _notificationsCache[idx] = Object.assign({}, _notificationsCache[idx], row);
  else _notificationsCache.unshift(row);
  _notificationsCache = _sortNotifications(_notificationsCache).slice(0, 200);
  _notificationsSeenKeys.add(key);
  return idx < 0;
}

function _setNotificationBadge(count) {
  const safe = Math.max(0, Number(count) || 0);
  _notificationsUnreadCount = safe;
  ['notificationsBadge', 'notificationsBadgeMobile', 'notificationsTitleBadge'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = safe > 99 ? '99+' : String(safe);
    el.hidden = safe <= 0;
    el.style.display = safe > 0 ? '' : 'none';
  });
}

function _notificationMetaText(row) {
  const bits = [];
  if (row && row.profile) bits.push(String(row.profile));
  if (row && row.job_id) bits.push('job ' + String(row.job_id));
  const when = _formatNotificationTime(row && row.created_at);
  if (when) bits.push(when);
  return bits.join(' · ');
}

function _renderNotificationList() {
  const box = document.getElementById('notificationsList');
  if (!box) return;
  if (!_notificationsCache.length) {
    box.innerHTML = '<div class="notifications-empty">No cron notifications yet.</div>';
    _renderNotificationDetail(null);
    return;
  }
  box.innerHTML = _notificationsCache.map(row => {
    const key = _notificationKey(row);
    const unread = row && !row.read_at;
    const selected = key && key === _notificationsSelectedKey;
    return `<button type="button" class="notification-row ${unread ? 'unread' : ''} ${selected ? 'active' : ''}" data-notification-key="${esc(key)}" onclick="openNotificationDetail(this.dataset.notificationKey)">
      <span class="notification-row-dot" aria-hidden="true"></span>
      <span class="notification-row-main">
        <span class="notification-row-title">${esc(_notificationTitle(row))}</span>
        <span class="notification-row-meta">${esc(_notificationMetaText(row))}</span>
        <span class="notification-row-preview">${esc(_notificationBody(row)).slice(0, 180)}</span>
      </span>
    </button>`;
  }).join('');
  if (!_notificationsSelectedKey || !_notificationsCache.some(row => _notificationKey(row) === _notificationsSelectedKey)) {
    _notificationsSelectedKey = _notificationKey(_notificationsCache[0]) || '';
  }
  _renderNotificationDetail(_notificationsCache.find(row => _notificationKey(row) === _notificationsSelectedKey) || null);
}

function _renderNotificationDetail(row) {
  const title = document.getElementById('notificationDetailTitle');
  const body = document.getElementById('notificationDetailBody');
  const empty = document.getElementById('notificationDetailEmpty');
  const readBtn = document.getElementById('btnNotificationRead');
  if (!body || !empty) return;
  if (!row) {
    if (title) title.textContent = '';
    if (readBtn) readBtn.style.display = 'none';
    body.style.display = 'none';
    body.innerHTML = '';
    empty.style.display = '';
    return;
  }
  if (title) title.textContent = _notificationTitle(row);
  if (readBtn) readBtn.style.display = row.read_at ? 'none' : '';
  empty.style.display = 'none';
  body.style.display = '';
  const media = Array.isArray(row.media) ? row.media : [];
  const outputPath = row.output_file || row.output_path || '';
  body.innerHTML = `<div class="main-view-content notification-detail-card">
    <div class="notification-detail-meta">${esc(_notificationMetaText(row))}</div>
    <pre class="notification-detail-body">${esc(_notificationBody(row))}</pre>
    ${outputPath ? `<div class="notification-detail-kv"><span>Output</span><code>${esc(outputPath)}</code></div>` : ''}
    ${row.job_id ? `<div class="notification-detail-actions"><button type="button" class="btn secondary" data-job-id="${esc(String(row.job_id))}" onclick="openNotificationCronJob(this.dataset.jobId)">Open scheduled job</button></div>` : ''}
    ${media.length ? `<div class="notification-detail-kv"><span>Media</span><code>${esc(String(media.length))} attachment${media.length === 1 ? '' : 's'}</code></div>` : ''}
    ${row.read_at ? `<div class="notification-detail-kv"><span>Read</span><code>${esc(_formatNotificationTime(row.read_at))}</code></div>` : ''}
  </div>`;
}

function _renderNotifications(summary) {
  const rows = summary && Array.isArray(summary.notifications) ? summary.notifications : _notificationsCache;
  _notificationsCache = _sortNotifications(rows);
  _notificationsSeenKeys = new Set(_notificationsCache.map(_notificationKey).filter(Boolean));
  _setNotificationBadge(summary && Number.isFinite(Number(summary.unread_count)) ? Number(summary.unread_count) : _notificationsCache.filter(n => !n.read_at).length);
  _renderNotificationList();
}

async function loadNotifications(force) {
  const refreshBtn = document.getElementById('notificationsRefreshBtn');
  if (refreshBtn && force) refreshBtn.classList.add('spinning');
  try {
    const data = await api('/api/notifications?' + new URLSearchParams(_notificationsApiParams({ limit: 100 })).toString(), { timeoutMs: 10000, retries: 1 });
    _renderNotifications(data || {});
    return data;
  } catch (e) {
    const box = document.getElementById('notificationsList');
    if (box && !_notificationsCache.length) box.innerHTML = '<div class="notifications-empty error">Could not load notifications.</div>';
    if (force && typeof showToast === 'function') showToast('Could not load notifications: ' + (e && e.message || e), 4000, 'error');
    return null;
  } finally {
    if (refreshBtn) refreshBtn.classList.remove('spinning');
  }
}

function openNotificationDetail(key) {
  _notificationsSelectedKey = String(key || '');
  _renderNotificationList();
}

async function markSelectedNotificationRead() {
  if (!_notificationsSelectedKey) return;
  const row = _notificationsCache.find(n => _notificationKey(n) === _notificationsSelectedKey);
  const notificationId = _notificationId(row);
  if (!notificationId) return;
  try {
    const payload = { id: notificationId };
    if (row && row.profile) payload.profile = row.profile;
    const data = await api('/api/notifications/read', { method: 'POST', body: JSON.stringify(payload) });
    if (data && data.notification) _mergeNotification(data.notification);
    await loadNotifications(false);
  } catch (e) {
    if (typeof showToast === 'function') showToast('Mark read failed: ' + (e && e.message || e), 4000, 'error');
  }
}

async function markAllNotificationsRead() {
  try {
    const url = '/api/notifications/read-all?' + new URLSearchParams(_notificationsApiParams()).toString();
    const data = await api(url, { method: 'POST', body: JSON.stringify({}) });
    if (data && data.summary) _renderNotifications(data.summary);
    else await loadNotifications(false);
  } catch (e) {
    if (typeof showToast === 'function') showToast('Mark all read failed: ' + (e && e.message || e), 4000, 'error');
  }
}

async function openNotificationCronJob(jobId) {
  const target = String(jobId || '');
  if (!target) return;
  try {
    if (typeof switchPanel === 'function') await switchPanel('tasks');
    if (typeof loadCrons === 'function') await loadCrons(false);
    if (typeof openCronDetail === 'function') openCronDetail(target);
  } catch (e) {
    if (typeof showToast === 'function') showToast('Could not open scheduled job: ' + (e && e.message || e), 4000, 'error');
  }
}

function _handleNotificationEvent(row) {
  const isNew = _mergeNotification(row);
  if (isNew && !(row && row.read_at)) _setNotificationBadge(_notificationsUnreadCount + 1);
  _renderNotificationList();
}

function startNotificationInboxStream() {
  if (typeof window === 'undefined') return;
  if (typeof EventSource === 'undefined') {
    loadNotifications(false).catch(() => {});
    return;
  }
  if (_notificationsEventSource) return;
  const url = _notificationsUrl('api/notifications/events', _notificationsApiParams());
  try {
    _notificationsEventSource = new EventSource(url.href, { withCredentials: true });
    _notificationsEventSource.addEventListener('notification', ev => {
      try { _handleNotificationEvent(JSON.parse(ev.data)); } catch (_) {}
    });
    _notificationsEventSource.addEventListener('snapshot', ev => {
      try { _renderNotifications(JSON.parse(ev.data)); } catch (_) {}
    });
    _notificationsEventSource.onerror = function() {
      try { _notificationsEventSource.close(); } catch (_) {}
      _notificationsEventSource = null;
      loadNotifications(false).catch(() => {});
      if (_notificationsReconnectTimer) return;
      _notificationsReconnectTimer = setTimeout(() => {
        _notificationsReconnectTimer = null;
        if (!document.hidden) startNotificationInboxStream();
      }, 15000);
    };
  } catch (_) {
    _notificationsEventSource = null;
  }
}

function stopNotificationInboxStream() {
  if (_notificationsReconnectTimer) {
    clearTimeout(_notificationsReconnectTimer);
    _notificationsReconnectTimer = null;
  }
  if (_notificationsEventSource) {
    try { _notificationsEventSource.close(); } catch (_) {}
    _notificationsEventSource = null;
  }
}

function _syncNotificationInboxVisibility() {
  if (document.hidden) stopNotificationInboxStream();
  else startNotificationInboxStream();
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', startNotificationInboxStream, { once: true });
  else startNotificationInboxStream();
  document.addEventListener('visibilitychange', _syncNotificationInboxVisibility);
}
