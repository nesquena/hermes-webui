// ── Tiling chat interface ──────────────────────────────────────────────────
// Managed by window.TILES. Each tile is a self-contained chat pane with its
// own session, messages, composer, and stream state. Tiles can be arranged in
// a CSS Grid, minimized to the tab bar, or closed.

(function(){
  const T = {
    tiles: [],          // {id, sid, session, messages, busy, activeStreamId, minimized, el}
    activeTileId: null,
    maxTiles: 6,
    nextId: 1,
    gridEl: null,
    tabBarEl: null,
    _tilingMode: false,
  };

  function tileById(id) { return T.tiles.find(t => t.id === id) || null; }
  function tileBySid(sid) { return T.tiles.find(t => t.sid === sid) || null; }
  function activeTile() { return tileById(T.activeTileId); }

  // ── DOM helpers ──────────────────────────────────────────────────────────

  function _createTileEl(tile) {
    const el = document.createElement('div');
    el.className = 'tile';
    el.dataset.tileId = String(tile.id);
    el.innerHTML =
      '<div class="tile-header">' +
        '<div class="tile-header-left">' +
          '<span class="tile-dot" hidden></span>' +
          '<span class="tile-title"></span>' +
        '</div>' +
        '<div class="tile-header-actions">' +
          '<button class="tile-btn tile-minimize-btn" data-tooltip="Minimize" aria-label="Minimize">' +
            '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="5" y1="12" x2="19" y2="12"/></svg>' +
          '</button>' +
          '<button class="tile-btn tile-maximize-btn" data-tooltip="Focus" aria-label="Focus">' +
            '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>' +
          '</button>' +
          '<button class="tile-btn tile-close-btn" data-tooltip="Close" aria-label="Close">' +
            '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
          '</button>' +
        '</div>' +
      '</div>' +
      '<div class="tile-body">' +
        '<div class="tile-messages-shell">' +
          '<div class="tile-empty-state">What can I help with?</div>' +
          '<div class="tile-messages" hidden></div>' +
        '</div>' +
      '</div>' +
      '<div class="tile-composer">' +
        '<div class="tile-composer-status"></div>' +
        '<div class="tile-composer-row">' +
          '<textarea class="tile-input" placeholder="Message …" rows="1"></textarea>' +
          '<button class="tile-send-btn" aria-label="Send">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>' +
          '</button>' +
        '</div>' +
      '</div>';

    // Wire controls
    el.querySelector('.tile-minimize-btn').onclick = () => minimizeTile(tile.id);
    el.querySelector('.tile-maximize-btn').onclick = () => focusTile(tile.id);
    el.querySelector('.tile-close-btn').onclick = () => closeTile(tile.id);
    el.querySelector('.tile-send-btn').onclick = () => _tileSend(tile.id);
    el.querySelector('.tile-input').addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _tileSend(tile.id); }
    });
    el.querySelector('.tile-input').addEventListener('input', function() {
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, 120) + 'px';
    });
    // Click body or header to focus
    el.querySelector('.tile-body').onclick = () => focusTile(tile.id);
    el.querySelector('.tile-header').addEventListener('click', e => {
      if (!e.target.closest('.tile-btn')) focusTile(tile.id);
    });

    return el;
  }

  function _createTabEl(tile) {
    const el = document.createElement('button');
    el.className = 'tile-tab';
    el.dataset.tileId = String(tile.id);
    el.innerHTML =
      '<span class="tile-tab-dot" hidden></span>' +
      '<span class="tile-tab-title"></span>' +
      '<span class="tile-tab-close">&times;</span>';
    el.querySelector('.tile-tab-title').onclick = () => restoreTile(tile.id);
    el.querySelector('.tile-tab-close').onclick = e => { e.stopPropagation(); closeTile(tile.id); };
    return el;
  }

  // ── Render ───────────────────────────────────────────────────────────────

  function _renderTileMessages(tile) {
    const shell = T.gridEl.querySelector(`.tile[data-tile-id="${tile.id}"] .tile-messages-shell`);
    if (!shell) return;
    const empty = shell.querySelector('.tile-empty-state');
    const container = shell.querySelector('.tile-messages');
    if (!container) return;

    const msgs = tile.messages || [];
    if (!msgs.length) {
      if (empty) empty.hidden = false;
      container.innerHTML = '';
      container.hidden = true;
      return;
    }
    if (empty) empty.hidden = true;
    container.hidden = false;

    // Use _createMessageElement if available, otherwise simplified fallback
    const createMsg = window._createMessageElement;
    container.innerHTML = '';
    for (const msg of msgs) {
      if (!msg || !msg.role || msg.role === 'tool') continue;
      if (typeof createMsg === 'function') {
        const el = createMsg(msg);
        if (el) { el.classList.add('tile-msg'); container.appendChild(el); }
      } else {
        const d = document.createElement('div');
        d.className = 'tile-msg tile-msg--' + (msg.role === 'user' ? 'user' : 'assistant');
        d.textContent = typeof msg.content === 'string' ? msg.content.slice(0, 500) : '(content)';
        container.appendChild(d);
      }
    }

    // Auto-scroll to bottom
    shell.scrollTop = shell.scrollHeight;
  }

  function _updateTileHeader(tile) {
    const el = T.gridEl.querySelector(`.tile[data-tile-id="${tile.id}"]`);
    if (!el) return;
    const title = tile.session ? (tile.session.title || 'New Chat') : 'No session';
    el.querySelector('.tile-title').textContent = title;
    el.querySelector('.tile-dot').hidden = !tile.busy;

    const tab = T.tabBarEl.querySelector(`.tile-tab[data-tile-id="${tile.id}"]`);
    if (tab) {
      tab.querySelector('.tile-tab-title').textContent = title;
      const d = tab.querySelector('.tile-tab-dot');
      if (d) d.hidden = !tile.busy;
    }
  }

  // ── Tile lifecycle ───────────────────────────────────────────────────────

  function openTileForSession(sid, sessionData) {
    if (!sid) return;
    // Reuse existing tile for this session
    const existing = tileBySid(sid);
    if (existing) { focusTile(existing.id); return; }

    // Enforce max — evict oldest idle (non-busy)
    if (T.tiles.length >= T.maxTiles) {
      const evict = T.tiles.find(t => !t.busy);
      if (evict) closeTile(evict.id);
      else return; // all busy — refuse
    }

    const id = T.nextId++;
    const tile = {
      id, sid,
      session: sessionData || null,
      messages: (sessionData && sessionData.messages) || [],
      busy: false,
      activeStreamId: null,
      minimized: false,
      el: null,
    };
    T.tiles.push(tile);

    const tileEl = _createTileEl(tile);
    tile.el = tileEl;
    T.gridEl.appendChild(tileEl);

    const tabEl = _createTabEl(tile);
    T.tabBarEl.appendChild(tabEl);

    _renderTileMessages(tile);
    _updateTileHeader(tile);
    _updateSidebarBadge(sid, 1);
    _refreshGrid();
    focusTile(tile.id);
  }

  function focusTile(id) {
    const tile = tileById(id);
    if (!tile) return;
    T.activeTileId = id;
    tile.minimized = false;

    for (const t of T.tiles) {
      if (t.el) t.el.classList.toggle('tile--hidden', t.minimized);
      if (t.el) t.el.classList.toggle('tile--focused', t.id === id);
      const tab = T.tabBarEl.querySelector(`.tile-tab[data-tile-id="${t.id}"]`);
      if (tab) tab.classList.toggle('tile-tab--active', t.id === id && !t.minimized);
    }

    // Sync global S state so existing code reads from the active tile
    if (tile.session) {
      if (typeof S !== 'undefined') {
        S.session = tile.session;
        S.messages = tile.messages;
        S.busy = tile.busy;
        S.activeStreamId = tile.activeStreamId;
      }
    }
    if (typeof syncTopbar === 'function') syncTopbar();
    if (typeof syncModelChip === 'function') syncModelChip();

    const input = tile.el.querySelector('.tile-input');
    if (input) setTimeout(() => input.focus(), 50);
  }

  function minimizeTile(id) {
    const tile = tileById(id);
    if (!tile) return;
    tile.minimized = true;
    if (tile.el) tile.el.classList.add('tile--hidden');
    const tab = T.tabBarEl.querySelector(`.tile-tab[data-tile-id="${id}"]`);
    if (tab) { tab.classList.remove('tile-tab--active'); tab.classList.add('tile-tab--minimized'); }

    if (T.activeTileId === id) {
      const next = T.tiles.find(t => !t.minimized);
      if (next) focusTile(next.id);
      else {
        T.activeTileId = null;
        if (typeof S !== 'undefined') { S.session = null; S.messages = []; S.busy = false; S.activeStreamId = null; }
        if (typeof syncTopbar === 'function') syncTopbar();
      }
    }
    _refreshGrid();
  }

  function restoreTile(id) {
    const tile = tileById(id);
    if (!tile) return;
    tile.minimized = false;
    if (tile.el) tile.el.classList.remove('tile--hidden');
    const tab = T.tabBarEl.querySelector(`.tile-tab[data-tile-id="${id}"]`);
    if (tab) tab.classList.remove('tile-tab--minimized');
    focusTile(id);
  }

  function closeTile(id) {
    const idx = T.tiles.findIndex(t => t.id === id);
    if (idx < 0) return;
    const tile = T.tiles[idx];
    // Cancel stream
    if (tile.busy && tile.activeStreamId && typeof cancelSessionStream === 'function') {
      cancelSessionStream(tile.session);
    }
    // Cleanup INFLIGHT
    if (tile.sid && typeof INFLIGHT !== 'undefined' && INFLIGHT[tile.sid]) {
      delete INFLIGHT[tile.sid];
      if (typeof clearInflightState === 'function') clearInflightState(tile.sid);
    }
    // Remove DOM
    if (tile.el) tile.el.remove();
    const tab = T.tabBarEl.querySelector(`.tile-tab[data-tile-id="${id}"]`);
    if (tab) tab.remove();
    T.tiles.splice(idx, 1);

    _updateSidebarBadge(tile.sid, -1);

    if (T.activeTileId === id) {
      T.activeTileId = null;
      const next = T.tiles.find(t => !t.minimized);
      if (next) focusTile(next.id);
      else {
        if (typeof S !== 'undefined') { S.session = null; S.messages = []; S.busy = false; S.activeStreamId = null; }
        const msgInner = document.getElementById('msgInner');
        if (msgInner) msgInner.innerHTML = '';
        if (typeof syncTopbar === 'function') syncTopbar();
      }
    }
    _refreshGrid();
  }

  // ── Grid layout ──────────────────────────────────────────────────────────

  function _refreshGrid() {
    const visible = T.tiles.filter(t => !t.minimized);
    const count = visible.length;
    T.gridEl.classList.toggle('tile-grid--empty', count === 0);
    T.tabBarEl.classList.toggle('tile-bar--hidden', !T.tiles.some(t => t.minimized));
    if (count <= 1) {
      T.gridEl.style.gridTemplateColumns = '1fr';
      T.gridEl.style.gridTemplateRows = '1fr';
    } else if (count === 2 || count === 4) {
      T.gridEl.style.gridTemplateColumns = '1fr 1fr';
      T.gridEl.style.gridTemplateRows = count > 2 ? '1fr 1fr' : '1fr';
    } else {
      T.gridEl.style.gridTemplateColumns = '1fr 1fr';
      T.gridEl.style.gridTemplateRows = '1fr 1fr';
    }
  }

  // ── Tile send ────────────────────────────────────────────────────────────

  async function _tileSend(tileId) {
    const tile = tileById(tileId);
    if (!tile) return;
    const input = tile.el.querySelector('.tile-input');
    const text = (input && input.value.trim()) || '';
    if (!text) return;
    if (input) { input.value = ''; input.style.height = 'auto'; }

    // If tile has no session, create one
    if (!tile.session) {
      try {
        const body = { model: window._defaultModel || '', model_provider: null, workspace: null, profile: 'default' };
        const data = await api('/api/session/new', { method: 'POST', body: JSON.stringify(body) });
        tile.session = data.session;
        tile.messages = data.session.messages || [];
        tile.sid = data.session.session_id;
        if (typeof S !== 'undefined') S.session = tile.session;
        if (typeof syncTopbar === 'function') syncTopbar();
        _updateTileHeader(tile);
      } catch(e) {
        if (typeof showToast === 'function') showToast('Failed to create session', 3000, 'error');
        return;
      }
    }

    // Push user message
    tile.messages.push({ role: 'user', content: text });
    if (typeof S !== 'undefined') S.messages = tile.messages;
    _renderTileMessages(tile);
    tile.busy = true;
    _updateTileHeader(tile);

    try {
      const body = {
        session_id: tile.sid,
        message: text,
        model: tile.session.model || window._defaultModel || '',
        model_provider: tile.session.model_provider || null,
        profile: 'default',
      };
      const res = await fetch(new URL('/api/chat', document.baseURI || location.href).href, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      tile.activeStreamId = data.stream_id || null;
      if (typeof S !== 'undefined') S.activeStreamId = tile.activeStreamId;
      if (typeof INFLIGHT !== 'undefined') {
        INFLIGHT[tile.sid] = { messages: [...tile.messages], uploaded: [], toolCalls: [] };
      }

      // Poll for stream completion
      const poll = setInterval(() => {
        if (typeof S !== 'undefined' && S.session && S.session.session_id === tile.sid && !S.busy) {
          clearInterval(poll);
          tile.messages = [...S.messages];
          tile.busy = false;
          tile.activeStreamId = null;
          _renderTileMessages(tile);
          _updateTileHeader(tile);
        }
      }, 500);
    } catch(e) {
      tile.busy = false;
      _updateTileHeader(tile);
      if (typeof showToast === 'function') showToast('Send failed: ' + (e.message || ''), 3000, 'error');
    }
  }

  // ── Sidebar badge ────────────────────────────────────────────────────────

  const _tileCounts = {};
  function _updateSidebarBadge(sid, delta) {
    if (!sid) return;
    _tileCounts[sid] = (_tileCounts[sid] || 0) + delta;
    const count = _tileCounts[sid];
    const row = document.querySelector(`[data-session-id="${CSS.escape(sid)}"]`);
    if (!row) return;
    let badge = row.querySelector('.session-tile-badge');
    if (count > 0) {
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'session-tile-badge';
        (row.querySelector('.session-row-right') || row.querySelector('.session-meta') || row).appendChild(badge);
      }
      badge.textContent = count > 9 ? '9+' : String(count);
    } else if (badge) {
      badge.remove();
    }
  }

  // ── Tiling mode toggle ───────────────────────────────────────────────────

  function toggleTilingMode() {
    T._tilingMode = !T._tilingMode;
    document.body.classList.toggle('tiling-mode', T._tilingMode);
    try { localStorage.setItem('hermes-tiling-mode', T._tilingMode ? '1' : '0'); } catch(_) {}
    if (typeof showToast === 'function') {
      showToast(T._tilingMode ? 'Tiling mode on — click sessions to open in new tiles' : 'Tiling mode off', 2500);
    }
    // Sync the titlebar button active state
    const btn = document.getElementById('btnTilingMode');
    if (btn) btn.classList.toggle('active', T._tilingMode);
  }

  function isTilingMode() { return !!T._tilingMode; }

  // ── Init ─────────────────────────────────────────────────────────────────

  function initTiles() {
    const mainChat = document.getElementById('mainChat');
    if (!mainChat) return;

    const grid = document.createElement('div');
    grid.id = 'tileGrid';
    grid.className = 'tile-grid tile-grid--empty';
    const tabBar = document.createElement('div');
    tabBar.id = 'tileBar';
    tabBar.className = 'tile-bar tile-bar--hidden';
    mainChat.appendChild(grid);
    mainChat.appendChild(tabBar);

    T.gridEl = grid;
    T.tabBarEl = tabBar;

    // Restore preference
    try {
      if (localStorage.getItem('hermes-tiling-mode') === '1') {
        setTimeout(() => { if (!isTilingMode()) toggleTilingMode(); }, 100);
      }
    } catch(_) {}
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initTiles);
  else initTiles();

  // Expose
  window.TILES = T;
  window.openTileForSession = openTileForSession;
  window.focusTile = focusTile;
  window.minimizeTile = minimizeTile;
  window.restoreTile = restoreTile;
  window.closeTile = closeTile;
  window.toggleTilingMode = toggleTilingMode;
  window.isTilingMode = isTilingMode;
})();
