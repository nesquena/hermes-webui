// ── Chat Tiling Extension ───────────────────────────────────────────
// Injects a floating toolbar with 3 layout preset buttons (2-column,
// 4-corners, 6-tile grid). Each tile owns its session, composer state,
// and can stream independently. Sidebar session clicks are intercepted
// at capture phase when the grid is active.
(() => {
  if (document.getElementById('ext-tiling-toolbar')) return;

  // ── State ──────────────────────────────────────────────────────────
  const T = {
    tiles: [],            // { id, sid, session, messages, busy, activeStreamId, el, maximized, composerText, modelVal }
    activeTileId: null,
    nextId: 1,
    gridEl: null,
    toolbarEl: null,
    gridVisible: false,
    maxTiles: 6,
    _busyWatcher: null,
    _tileCounts: {},      // sid → open tile count
  };

  // ── Helpers ────────────────────────────────────────────────────────
  function tileById(id) { return T.tiles.find(t => t.id === id) || null; }
  function tileBySid(sid) { return T.tiles.find(t => t.sid === sid) || null; }
  function activeTile() { return tileById(T.activeTileId); }

  // ── Settings ────────────────────────────────────────────────────────
  function getSetting(key, def) {
    try {
      if (window.HermesExtensionSettings) {
        const s = window.HermesExtensionSettings.settingsForExtension('chat-tiling');
        return s.get(key) != null ? s.get(key) : def;
      }
    } catch(_) {}
    return def;
  }

  // ── Composer save/restore ───────────────────────────────────────────
  function saveComposerTo(tile) {
    if (!tile) return;
    const msg = document.getElementById('msg');
    if (msg) tile.composerText = msg.value;
    const modelSel = document.getElementById('modelSelect');
    if (modelSel) tile.modelVal = modelSel.value;
  }

  function restoreComposerFrom(tile) {
    if (!tile) return;
    const msg = document.getElementById('msg');
    if (msg && tile.composerText) msg.value = tile.composerText;
    if (typeof triggerMsgh === 'function') triggerMsgh();
    const modelSel = document.getElementById('modelSelect');
    if (modelSel && tile.modelVal && tile.modelVal !== modelSel.value) {
      modelSel.value = tile.modelVal;
      if (typeof _onModelSelectChange === 'function') _onModelSelectChange();
    }
  }

  // ── DOM creation ────────────────────────────────────────────────────
  function createTileEl(tile) {
    const el = document.createElement('div');
    el.className = 'ext-tile';
    el.dataset.tileId = String(tile.id);
    el.innerHTML =
      '<div class="ext-tile-header">' +
        '<div class="ext-tile-header-left">' +
          '<span class="ext-tile-dot" hidden></span>' +
          '<span class="ext-tile-title"></span>' +
        '</div>' +
        '<div class="ext-tile-header-actions">' +
          '<button class="ext-tile-btn ext-tile-maximize-btn" title="Maximize" aria-label="Maximize">' +
            '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>' +
          '</button>' +
          '<button class="ext-tile-btn ext-tile-unmaximize-btn" title="Restore" aria-label="Restore" hidden>' +
            '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg>' +
          '</button>' +
          '<button class="ext-tile-btn ext-tile-close-btn" title="Close" aria-label="Close">' +
            '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
          '</button>' +
        '</div>' +
      '</div>' +
      '<div class="ext-tile-body">' +
        '<div class="ext-tile-msg-inner"></div>' +
      '</div>';

    el.querySelector('.ext-tile-maximize-btn').onclick = (e) => { e.stopPropagation(); maximizeTile(tile.id); };
    el.querySelector('.ext-tile-unmaximize-btn').onclick = (e) => { e.stopPropagation(); unmaximizeTile(tile.id); };
    el.querySelector('.ext-tile-close-btn').onclick = (e) => { e.stopPropagation(); closeTile(tile.id); };
    el.querySelector('.ext-tile-body').addEventListener('click', () => focusTile(tile.id));
    el.querySelector('.ext-tile-header').addEventListener('click', e => {
      if (!e.target.closest('.ext-tile-btn')) focusTile(tile.id);
    });

    return el;
  }

  function updateTileHeader(tile) {
    const el = tile.el || (T.gridEl && T.gridEl.querySelector(`.ext-tile[data-tile-id="${tile.id}"]`));
    if (!el) return;
    const title = tile.session ? (tile.session.title || 'New Chat') : '';
    const titleEl = el.querySelector('.ext-tile-title');
    if (titleEl) titleEl.textContent = title || 'Empty tile';
    const dot = el.querySelector('.ext-tile-dot');
    if (dot) dot.hidden = !tile.busy;
  }

  // ── Focus switching ─────────────────────────────────────────────────
  function focusTile(id) {
    const tile = tileById(id);
    if (!tile) return;

    // Save state from old focused tile
    if (T.activeTileId && T.activeTileId !== id) {
      const oldTile = activeTile();
      if (oldTile) {
        saveComposerTo(oldTile);
        if (typeof S !== 'undefined') {
          oldTile.messages = [...(S.messages || [])];
          oldTile.busy = !!S.busy;
          oldTile.activeStreamId = S.activeStreamId || null;
          oldTile.session = S.session;
        }
      }
    }

    // Move #msgInner id to the new focused tile's container
    const cur = document.getElementById('msgInner');
    if (cur) cur.removeAttribute('id');

    T.activeTileId = id;

    // Highlight focused tile
    for (const t of T.tiles) {
      if (t.el) t.el.classList.toggle('ext-tile--focused', t.id === id);
    }

    // Set this tile's container as the render target
    const newInner = tile.el && tile.el.querySelector('.ext-tile-msg-inner');
    if (newInner) {
      newInner.id = 'msgInner';
    }

    // Sync global S
    if (typeof S !== 'undefined') {
      S.session = tile.session;
      S.messages = tile.messages || [];
      S.busy = tile.busy || false;
      S.activeStreamId = tile.activeStreamId || null;
    }

    restoreComposerFrom(tile);

    if (typeof syncTopbar === 'function') syncTopbar();
    if (typeof syncModelChip === 'function') syncModelChip();
    updateTileHeader(tile);
    startBusyWatcher();
  }

  // ── Open tile for a session ─────────────────────────────────────────
  function openTileForSession(sid, sessionData) {
    if (!sid) return;
    const existing = tileBySid(sid);
    if (existing) { focusTile(existing.id); return; }

    // Find first empty tile
    let targetTile = T.tiles.find(t => !t.sid);
    if (!targetTile) {
      // No empty tile — show toast
      if (typeof showToast === 'function') showToast('All tiles are in use. Close one first.', 3000, 'error');
      return;
    }

    const msgs = (sessionData && sessionData.messages) || [];
    targetTile.sid = sid;
    targetTile.session = sessionData || null;
    targetTile.messages = msgs;
    targetTile.composerText = '';
    targetTile.modelVal = null;

    updateTileHeader(targetTile);
    updateSidebarBadge(sid, 1);
    renderMessagesToTile(targetTile);
    focusTile(targetTile.id);

    // Load full session if partial data
    if (!msgs.length && sid) {
      (async () => {
        try {
          const full = await window.api(`/api/session?session_id=${encodeURIComponent(sid)}&resolve_model=0`);
          if (full && full.messages) {
            targetTile.messages = full.messages || [];
            targetTile.session = full;
            if (T.activeTileId === targetTile.id) {
              if (typeof S !== 'undefined') { S.messages = targetTile.messages; S.session = targetTile.session; }
              renderMessagesToTile(targetTile);
            }
            updateTileHeader(targetTile);
          }
        } catch(_) {}
      })();
    }
  }

  // ── Render messages ─────────────────────────────────────────────────
  function renderMessagesToTile(tile) {
    const mi = tile.el && tile.el.querySelector('.ext-tile-msg-inner');
    if (!mi) return;
    mi.innerHTML = '';
    const createMsg = window._createMessageElement;
    const msgs = tile.messages || [];
    for (const msg of msgs) {
      if (!msg || !msg.role || msg.role === 'tool') continue;
      if (typeof createMsg === 'function') {
        const el = createMsg(msg);
        if (el) mi.appendChild(el);
      } else {
        const d = document.createElement('div');
        d.textContent = typeof msg.content === 'string' ? msg.content.slice(0, 500) : '(content)';
        mi.appendChild(d);
      }
    }
    if (mi.scrollTop !== undefined) mi.scrollTop = mi.scrollHeight;
  }

  // ── Maximize / Unmaximize ───────────────────────────────────────────
  function maximizeTile(id) {
    const tile = tileById(id);
    if (!tile) return;
    if (tile.maximized) { unmaximizeTile(id); return; }
    const curMax = T.tiles.find(t => t.maximized);
    if (curMax) {
      curMax.maximized = false;
      if (curMax.el) {
        curMax.el.classList.remove('ext-tile--maximized');
        curMax.el.querySelector('.ext-tile-maximize-btn').hidden = false;
        curMax.el.querySelector('.ext-tile-unmaximize-btn').hidden = true;
      }
    }
    tile.maximized = true;
    if (tile.el) {
      tile.el.classList.add('ext-tile--maximized');
      tile.el.querySelector('.ext-tile-maximize-btn').hidden = true;
      tile.el.querySelector('.ext-tile-unmaximize-btn').hidden = false;
    }
    for (const t of T.tiles) {
      if (t.el) t.el.classList.toggle('ext-tile--hidden', !t.maximized);
    }
    refreshGrid();
  }

  function unmaximizeTile(id) {
    const tile = tileById(id);
    if (!tile) return;
    tile.maximized = false;
    if (tile.el) {
      tile.el.classList.remove('ext-tile--maximized');
      tile.el.querySelector('.ext-tile-maximize-btn').hidden = false;
      tile.el.querySelector('.ext-tile-unmaximize-btn').hidden = true;
    }
    for (const t of T.tiles) {
      if (t.el) t.el.classList.remove('ext-tile--hidden');
    }
    refreshGrid();
  }

  // ── Close ───────────────────────────────────────────────────────────
  function closeTile(id) {
    const idx = T.tiles.findIndex(t => t.id === id);
    if (idx < 0) return;
    const tile = T.tiles[idx];

    if (tile.busy && tile.activeStreamId && typeof cancelSessionStream === 'function') {
      cancelSessionStream(tile.session);
    }
    if (tile.session && typeof INFLIGHT !== 'undefined' && INFLIGHT[tile.session.session_id]) {
      delete INFLIGHT[tile.session.session_id];
      if (typeof clearInflightState === 'function') clearInflightState(tile.session.session_id);
    }

    if (tile.el) {
      const mi = tile.el.querySelector('.ext-tile-msg-inner');
      if (mi && mi.id === 'msgInner') mi.removeAttribute('id');
      tile.el.remove();
    }
    T.tiles.splice(idx, 1);

    if (tile.maximized) {
      for (const t of T.tiles) {
        t.maximized = false;
        if (t.el) {
          t.el.classList.remove('ext-tile--hidden', 'ext-tile--maximized');
          t.el.querySelector('.ext-tile-maximize-btn').hidden = false;
          t.el.querySelector('.ext-tile-unmaximize-btn').hidden = true;
        }
      }
    }

    if (tile.sid) updateSidebarBadge(tile.sid, -1);

    if (T.activeTileId === id) {
      T.activeTileId = null;
      const next = T.tiles[0];
      if (next) focusTile(next.id);
      else {
        hideGrid();
      }
    }
    refreshGrid();
    updateToolbarActiveState();
  }

  // ── Grid layout ─────────────────────────────────────────────────────
  function refreshGrid() {
    if (!T.gridEl) return;
    const count = T.tiles.length;
    T.gridEl.classList.toggle('ext-tile-grid--empty', count === 0);

    // Use the stored layout columns/rows
    if (T._layoutCols && T._layoutRows) {
      T.gridEl.style.gridTemplateColumns = `repeat(${T._layoutCols}, 1fr)`;
      T.gridEl.style.gridTemplateRows = `repeat(${T._layoutRows}, 1fr)`;
    }
  }

  // ── Busy watcher ────────────────────────────────────────────────────
  function startBusyWatcher() {
    stopBusyWatcher();
    T._busyWatcher = setInterval(() => {
      const tile = activeTile();
      if (!tile || T.activeTileId === null) { stopBusyWatcher(); return; }
      if (typeof S !== 'undefined') {
        if (S.messages && S.messages.length > 0) tile.messages = [...S.messages];
        tile.busy = !!S.busy;
        tile.activeStreamId = S.activeStreamId || null;
        if (!S.busy && tile.session) tile.session = S.session;
      }
      updateTileHeader(tile);
    }, 500);
  }

  function stopBusyWatcher() {
    if (T._busyWatcher) { clearInterval(T._busyWatcher); T._busyWatcher = null; }
  }

  // ── Sidebar badge ───────────────────────────────────────────────────
  function updateSidebarBadge(sid, delta) {
    if (!sid) return;
    T._tileCounts[sid] = (T._tileCounts[sid] || 0) + delta;
    const count = T._tileCounts[sid];
    const row = document.querySelector(`[data-session-id="${sid}"]`);
    if (!row) return;
    let badge = row.querySelector('.ext-tile-sidebar-badge');
    if (count > 0 && getSetting('show_sidebar_badges', true)) {
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'ext-tile-sidebar-badge';
        (row.querySelector('.session-row-right') || row.querySelector('.session-meta') || row).appendChild(badge);
      }
      badge.textContent = count > 9 ? '9+' : String(count);
    } else if (badge) {
      badge.remove();
    }
  }

  // ── Show / Hide grid ────────────────────────────────────────────────
  function showGrid(cols, rows) {
    if (T.gridVisible) {
      // Already visible — if changing layout, close all and re-open
      if (T._layoutCols !== cols || T._layoutRows !== rows) {
        closeAllTiles();
      } else return;
    }

    T._layoutCols = cols;
    T._layoutRows = rows;
    const count = cols * rows;
    T.gridVisible = true;

    // Hide original msgInner
    const origMsgInner = document.getElementById('msgInner');
    if (origMsgInner) {
      origMsgInner.removeAttribute('id');
      origMsgInner.classList.add('messages-inner--idle');
    }

    // Set body class for CSS
    document.body.classList.add('ext-tiling-body');

    // Show grid
    T.gridEl.style.display = '';
    T.gridEl.classList.add('ext-tile-grid--active');
    T.gridEl.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
    T.gridEl.style.gridTemplateRows = `repeat(${rows}, 1fr)`;

    // Close old tiles if any
    closeAllTiles();

    // Create N empty tiles
    for (let i = 0; i < count; i++) {
      const tile = {
        id: T.nextId++,
        sid: null,
        session: null,
        messages: [],
        busy: false,
        activeStreamId: null,
        maximized: false,
        el: null,
        composerText: '',
        modelVal: null,
      };
      T.tiles.push(tile);
      const tileEl = createTileEl(tile);
      tile.el = tileEl;
      T.gridEl.appendChild(tileEl);
      updateTileHeader(tile);
    }

    refreshGrid();
    // Focus first tile
    if (T.tiles.length > 0) focusTile(T.tiles[0].id);

    updateToolbarActiveState();

    // Persist
    try { localStorage.setItem('hermes-ext-tiling-layout', `${cols}x${rows}`); } catch(_) {}
  }

  function hideGrid() {
    T.gridVisible = false;
    stopBusyWatcher();

    // Remove id from any tile container
    document.querySelectorAll('.ext-tile-msg-inner[id="msgInner"]').forEach(el => el.removeAttribute('id'));

    // Restore #msgInner to the original element
    const origMsgInner = document.querySelector('#messages > .messages-inner--idle');
    if (origMsgInner) {
      origMsgInner.id = 'msgInner';
      origMsgInner.classList.remove('messages-inner--idle');
    }

    document.body.classList.remove('ext-tiling-body');

    closeAllTiles();

    T.gridEl.style.display = 'none';
    T.gridEl.classList.remove('ext-tile-grid--active');

    // Show empty state if the original chat area is empty
    const es = document.getElementById('emptyState');
    if (es) es.style.display = '';
    if (typeof checkEmptyState === 'function') checkEmptyState();

    if (typeof S !== 'undefined') {
      S.session = null; S.messages = []; S.busy = false; S.activeStreamId = null;
    }
    if (typeof syncTopbar === 'function') syncTopbar();

    updateToolbarActiveState();

    // Clear persisted layout
    try { localStorage.removeItem('hermes-ext-tiling-layout'); } catch(_) {}
  }

  function closeAllTiles() {
    for (const tile of [...T.tiles]) {
      if (tile.el) {
        const mi = tile.el.querySelector('.ext-tile-msg-inner');
        if (mi && mi.id === 'msgInner') mi.removeAttribute('id');
        tile.el.remove();
      }
    }
    T.tiles = [];
    T.activeTileId = null;
    T._tileCounts = {};
    // Remove all sidebar badges
    document.querySelectorAll('.ext-tile-sidebar-badge').forEach(b => b.remove());
  }

  // ── Sidebar click interception ───────────────────────────────────────
  // Use capture phase to intercept sidebar session clicks before core handlers
  function initSidebarCapture() {
    // Watch for the sidebar container to appear
    const observer = new MutationObserver(() => {
      const sidebar = document.getElementById('sessionSidebar') || document.querySelector('.sidebar-session-list');
      if (sidebar && !sidebar.dataset.extTilingWired) {
        sidebar.dataset.extTilingWired = '1';
        sidebar.addEventListener('click', onSidebarClick, true);
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });

    // Also try immediately
    setTimeout(() => {
      const sidebar = document.getElementById('sessionSidebar') || document.querySelector('.sidebar-session-list');
      if (sidebar && !sidebar.dataset.extTilingWired) {
        sidebar.dataset.extTilingWired = '1';
        sidebar.addEventListener('click', onSidebarClick, true);
      }
    }, 1000);
  }

  function onSidebarClick(e) {
    if (!T.gridVisible) return; // Not in grid mode — let core handle it

    const row = e.target.closest('[data-session-id]');
    if (!row) return;

    const sid = row.dataset.sessionId;
    if (!sid) return;

    // Stop core loadSession from running
    e.stopPropagation();
    e.preventDefault();

    // Open in tile
    (async () => {
      try {
        const data = await window.api(`/api/session?session_id=${encodeURIComponent(sid)}&resolve_model=0`);
        openTileForSession(sid, data);
      } catch(_) {
        if (typeof showToast === 'function') showToast('Failed to load session', 3000, 'error');
      }
    })();
  }

  // ── Toolbar ──────────────────────────────────────────────────────────
  function createToolbar() {
    const tb = document.createElement('div');
    tb.id = 'ext-tiling-toolbar';
    tb.innerHTML =
      '<div class="ext-toolbar-label">Tiles</div>' +
      // 2-column layout
      '<button class="ext-toolbar-btn" data-tooltip="Split 2 (horizontal)" aria-label="Split in 2" data-layout="2x1">' +
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="8" height="18" rx="1"/><rect x="13" y="3" width="8" height="18" rx="1"/></svg>' +
      '</button>' +
      // 4 corners
      '<button class="ext-toolbar-btn" data-tooltip="Split 4 (2×2 corners)" aria-label="Split in 4" data-layout="2x2">' +
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="8" rx="1"/><rect x="3" y="13" width="8" height="8" rx="1"/><rect x="13" y="13" width="8" height="8" rx="1"/></svg>' +
      '</button>' +
      // 6 grid (3 columns × 2 rows)
      '<button class="ext-toolbar-btn" data-tooltip="Split 6 (3×2 grid)" aria-label="Split in 6" data-layout="3x2">' +
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="3" width="5" height="8" rx="1"/><rect x="8.5" y="3" width="5" height="8" rx="1"/><rect x="15" y="3" width="5" height="8" rx="1"/><rect x="2" y="13" width="5" height="8" rx="1"/><rect x="8.5" y="13" width="5" height="8" rx="1"/><rect x="15" y="13" width="5" height="8" rx="1"/></svg>' +
      '</button>' +
      '<div class="ext-toolbar-divider"></div>' +
      // Close button
      '<button class="ext-toolbar-btn" data-tooltip="Close all tiles" aria-label="Close tiling" data-layout="close">' +
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
      '</button>';

    const titlebar = document.querySelector('header.app-titlebar');
    if (titlebar) titlebar.appendChild(tb);
    else document.body.appendChild(tb);

    // Wire click handlers
    tb.querySelectorAll('.ext-toolbar-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const layout = btn.dataset.layout;
        if (layout === 'close') {
          hideGrid();
        } else {
          const [cols, rows] = layout.split('x').map(Number);
          if (T.gridVisible && T._layoutCols === cols && T._layoutRows === rows) {
            // Clicking same layout again — hide
            hideGrid();
          } else {
            showGrid(cols, rows);
          }
        }
      });
    });

    T.toolbarEl = tb;
  }

  function updateToolbarActiveState() {
    if (!T.toolbarEl) return;
    T.toolbarEl.classList.toggle('ext-tiling-toolbar--visible', true);

    // Highlight active layout button
    T.toolbarEl.querySelectorAll('.ext-toolbar-btn').forEach(btn => {
      if (btn.dataset.layout === 'close') return;
      const [cols, rows] = btn.dataset.layout.split('x').map(Number);
      const active = T.gridVisible && T._layoutCols === cols && T._layoutRows === rows;
      btn.classList.toggle('ext-toolbar-btn--active', active);
    });
  }

  // ── Keyboard shortcut ───────────────────────────────────────────────
  function initKeyboard() {
    document.addEventListener('keydown', e => {
      // Ctrl+Shift+T: default layout
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'T' && !e.repeat) {
        e.preventDefault();
        if (T.gridVisible) {
          hideGrid();
        } else {
          const def = getSetting('default_layout', '4');
          const [cols, rows] = ({ '2': [2,1], '4': [2,2], '6': [3,2] })[def] || [2, 2];
          showGrid(cols, rows);
        }
      }
      // Ctrl+Shift+2, 4, 6: specific layouts
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && !e.repeat) {
        const map = { '2': [2,1], '4': [2,2], '6': [3,2] };
        if (map[e.key]) {
          e.preventDefault();
          const [c, r] = map[e.key];
          if (T.gridVisible && T._layoutCols === c && T._layoutRows === r) {
            hideGrid();
          } else {
            showGrid(c, r);
          }
        }
      }
    });
  }

  // ── Export for compatibility with old core intercept ─────────────────
  window.openTileForSessionExt = openTileForSession;
  window.focusTileExt = focusTile;
  window.closeTileExt = closeTile;
  window.maximizeTileExt = maximizeTile;
  window.unmaximizeTileExt = unmaximizeTile;

  // ── Init ─────────────────────────────────────────────────────────────
  function init() {
    // Create grid container
    T.gridEl = document.createElement('div');
    T.gridEl.id = 'ext-tile-grid';
    T.gridEl.className = 'ext-tile-grid';
    T.gridEl.style.display = 'none';

    const msgInner = document.getElementById('msgInner');
    if (msgInner && msgInner.parentNode) {
      msgInner.parentNode.appendChild(T.gridEl);
    }

    createToolbar();
    updateToolbarActiveState();
    initSidebarCapture();
    initKeyboard();

    // Restore persisted layout
    try {
      const saved = localStorage.getItem('hermes-ext-tiling-layout');
      if (saved) {
        const [cols, rows] = saved.split('x').map(Number);
        if (cols && rows && cols * rows <= 6) {
          setTimeout(() => showGrid(cols, rows), 500);
        }
      }
    } catch(_) {}
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
