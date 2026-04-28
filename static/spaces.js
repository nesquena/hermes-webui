// Capy Spaces foundation shell.
// This UI exposes safe metadata and widget management without executing widget renderers.
(function(){
  var handlersBound = false;

  async function fetchSpacesJson(path, options){
    const res = await fetch(path, Object.assign({cache: 'no-store'}, options || {}));
    if (!res.ok) throw new Error('Spaces request failed: '+res.status);
    return res.json();
  }

  async function postSpacesJson(path, body){
    return fetchSpacesJson(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {}),
    });
  }

  async function loadCapySpaces(){
    const root = document.getElementById('capySpacesRoot');
    if (!root) return;
    ensureCapySpacesHandlers();
    try {
      const data = await fetchSpacesJson('api/spaces');
      if (!data.enabled) {
        root.innerHTML = '<div class="capy-spaces-card"><h3>Capy Spaces disabled</h3><div class="capy-spaces-muted">Set HERMES_WEBUI_SPACES_ENABLED=1 to enable the foundation shell.</div></div>';
        return;
      }
      root.dataset.editingSpaceId = '';
      const spaces = data.spaces || [];
      root.innerHTML = renderSpacesList(spaces);
    } catch (err) {
      root.innerHTML = '<div class="capy-spaces-card"><h3>Capy Spaces unavailable</h3><div class="capy-spaces-muted">'+escapeHtml(err.message||String(err))+'</div></div>';
    }
  }

  function renderSpacesList(spaces){
    const activeSpaceId = currentActiveSpaceId();
    const cards = spaces.length ? spaces.map(function(s){
      const spaceId = s.space_id || '';
      const name = s.name || spaceId || 'Untitled';
      const description = s.description || '';
      const activeLabel = activeSpaceId && activeSpaceId === spaceId ? ' · Active in chat' : '';
      return '<div class="capy-spaces-card" data-space-id="'+escapeHtml(spaceId)+'">' +
        '<div class="capy-spaces-card-row"><div><strong>'+escapeHtml(name)+'</strong>' +
        (description ? '<div class="capy-spaces-muted">'+escapeHtml(description)+'</div>' : '') +
        '<div class="capy-spaces-muted">Widgets: '+Number(s.widget_count||0)+' · Revision: '+escapeHtml(s.revision_event_id||'none')+escapeHtml(activeLabel)+'</div></div>' +
        '<div class="capy-spaces-actions">' +
        '<button type="button" class="capy-spaces-btn" data-capy-action="openSpace" data-space-id="'+escapeHtml(spaceId)+'">Open</button>' +
        '<button type="button" class="capy-spaces-btn" data-capy-action="activateSpace" data-space-id="'+escapeHtml(spaceId)+'">Use in chat</button>' +
        '<button type="button" class="capy-spaces-btn" data-capy-action="editSpace" data-space-id="'+escapeHtml(spaceId)+'" data-space-name="'+escapeHtml(name)+'" data-space-description="'+escapeHtml(description)+'">Edit</button>' +
        '<button type="button" class="capy-spaces-btn" data-capy-action="loadWidgets" data-space-id="'+escapeHtml(spaceId)+'">Manage widgets</button>' +
        '<button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="deleteSpace" data-space-id="'+escapeHtml(spaceId)+'">Delete</button>' +
        '</div></div>' +
        '</div>';
    }).join('') : '<div class="capy-spaces-card"><strong>No spaces yet</strong><div class="capy-spaces-muted">Create a space below to start adding safe metadata-only widgets.</div></div>';
    return '<div class="capy-spaces-card"><h3>Capy Spaces</h3><div class="capy-spaces-muted">'+spaces.length+' space(s). Widget management lists metadata only; generated widget code is not executed here.</div></div>' +
      cards + renderSpaceForm();
  }

  function renderSpaceForm(){
    return '<div class="capy-spaces-card"><h3>Create or edit a space</h3>' +
      '<div class="capy-spaces-muted">Space IDs are path-safe identifiers. Editing keeps the original ID and updates metadata only.</div>' +
      '<div class="capy-spaces-form" aria-label="Create or update space">' +
      '<label>Space ID<input id="capySpaceId" type="text" autocomplete="off" placeholder="daily-ops"></label>' +
      '<label>Name<input id="capySpaceName" type="text" autocomplete="off" placeholder="Daily Ops"></label>' +
      '<label>Description<input id="capySpaceDescription" type="text" autocomplete="off" placeholder="Operational dashboard"></label>' +
      '<button type="button" class="capy-spaces-btn" data-capy-action="saveSpace">Save space</button>' +
      '<button type="button" class="capy-spaces-btn" data-capy-action="newSpace">New space</button>' +
      '</div></div>';
  }

  async function loadSpaceWidgets(spaceId){
    const root = document.getElementById('capySpacesRoot');
    if (!root) return;
    ensureCapySpacesHandlers();
    const safeSpaceId = String(spaceId || '').trim();
    if (!safeSpaceId) return;
    try {
      const data = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(safeSpaceId));
      root.innerHTML = renderWidgetManager(safeSpaceId, data.widgets || []);
    } catch (err) {
      root.innerHTML = '<div class="capy-spaces-card"><h3>Widget manager unavailable</h3><div class="capy-spaces-muted">'+escapeHtml(err.message||String(err))+'</div><button type="button" class="capy-spaces-btn" data-capy-action="reloadSpaces">Back to spaces</button></div>';
    }
  }

  async function openSpaceDetail(spaceId){
    const root = document.getElementById('capySpacesRoot');
    if (!root) return;
    ensureCapySpacesHandlers();
    const safeSpaceId = String(spaceId || '').trim();
    if (!safeSpaceId) return;
    try {
      const results = await Promise.all([
        fetchSpacesJson('api/spaces/get?space_id='+encodeURIComponent(safeSpaceId)),
        fetchSpacesJson('api/spaces/revisions?space_id='+encodeURIComponent(safeSpaceId)),
      ]);
      root.innerHTML = renderSpaceDetail(results[0].space || {}, results[1].revisions || []);
    } catch (err) {
      root.innerHTML = '<div class="capy-spaces-card"><h3>Space detail unavailable</h3><div class="capy-spaces-muted">'+escapeHtml(err.message||String(err))+'</div><button type="button" class="capy-spaces-btn" data-capy-action="reloadSpaces">Back to spaces</button></div>';
    }
  }

  function renderSpaceDetail(space, revisions){
    const spaceId = space.space_id || '';
    const name = space.name || spaceId || 'Untitled';
    const description = space.description || '';
    const widgets = Array.isArray(space.widgets) ? space.widgets : [];
    const widgetRows = widgets.length ? widgets.map(function(w){
      const widgetId = w.id || '';
      const layout = widgetLayout(w);
      return '<div class="capy-spaces-widget" data-widget-id="'+escapeHtml(widgetId)+'"><div><strong>'+escapeHtml(w.title||widgetId||'Untitled widget')+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(w.kind||'custom')+' · '+escapeHtml(widgetId)+' · '+escapeHtml(formatWidgetLayout(layout))+'</div></div></div>';
    }).join('') : '<div class="capy-spaces-muted">No widgets yet.</div>';
    return '<div class="capy-spaces-card"><button type="button" class="capy-spaces-btn" data-capy-action="reloadSpaces">← Back to spaces</button>' +
      '<h3>'+escapeHtml(name)+'</h3>' +
      (description ? '<div class="capy-spaces-muted">'+escapeHtml(description)+'</div>' : '') +
      '<div class="capy-spaces-muted">Space ID: '+escapeHtml(spaceId)+' · Revision: '+escapeHtml(space.revision_event_id||'none')+'</div>' +
      '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn" data-capy-action="activateSpace" data-space-id="'+escapeHtml(spaceId)+'">Use in chat</button><button type="button" class="capy-spaces-btn" data-capy-action="loadWidgets" data-space-id="'+escapeHtml(spaceId)+'">Manage widgets</button></div>' +
      '</div><div class="capy-spaces-card"><h3>Widgets</h3><div class="capy-spaces-muted">Metadata-only detail view. Generated widget code is intentionally not displayed or executed.</div><div class="capy-spaces-widget-list">'+widgetRows+'</div></div>' +
      renderRevisionHistory(revisions || []);
  }

  function renderRevisionHistory(revisions){
    const safeRevisions = Array.isArray(revisions) ? revisions : [];
    const rows = safeRevisions.length ? safeRevisions.slice(0, 10).map(function(rev){
      const eventId = rev && rev.event_id ? String(rev.event_id) : '';
      const eventType = rev && rev.event_type ? String(rev.event_type) : 'unknown';
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(eventType)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(formatRevisionTime(rev && rev.created_at))+' · '+escapeHtml(eventId.slice(0, 12) || 'no-event-id')+'</div></div></div>';
    }).join('') : '<div class="capy-spaces-muted">No revision events recorded yet.</div>';
    return '<div class="capy-spaces-card"><h3>Revision history</h3>' +
      '<div class="capy-spaces-muted">Newest safe metadata events. Rollback controls will build on this index; generated widget bodies are not displayed.</div>' +
      '<div class="capy-spaces-widget-list">'+rows+'</div></div>';
  }

  function formatRevisionTime(value){
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds <= 0) return 'time unknown';
    try {
      return new Date(seconds * 1000).toISOString().replace('T', ' ').replace('.000Z', ' UTC');
    } catch (err) {
      return 'time unknown';
    }
  }

  function renderWidgetManager(spaceId, widgets){
    const widgetCards = widgets.length ? widgets.map(function(w){
      const widgetId = w.id || '';
      const title = w.title || widgetId || 'Untitled widget';
      const kind = w.kind || 'custom';
      const layout = widgetLayout(w);
      return '<div class="capy-spaces-widget" data-widget-id="'+escapeHtml(widgetId)+'">' +
        '<div><strong>'+escapeHtml(title)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(kind)+' · '+escapeHtml(widgetId)+' · '+escapeHtml(formatWidgetLayout(layout))+'</div></div>' +
        '<div class="capy-spaces-actions">' +
        '<button type="button" class="capy-spaces-btn" data-capy-action="askWidget" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'" data-widget-title="'+escapeHtml(title)+'">Ask Capy</button>' +
        '<button type="button" class="capy-spaces-btn" data-capy-action="editWidget" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'" data-widget-title="'+escapeHtml(title)+'" data-widget-kind="'+escapeHtml(kind)+'" data-widget-x="'+escapeHtml(layout.x)+'" data-widget-y="'+escapeHtml(layout.y)+'" data-widget-w="'+escapeHtml(layout.w)+'" data-widget-h="'+escapeHtml(layout.h)+'">Edit</button>' +
        '<button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="deleteWidget" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'">Delete</button>' +
        '</div></div>';
    }).join('') : '<div class="capy-spaces-muted">No widgets yet.</div>';
    return '<div class="capy-spaces-card"><button type="button" class="capy-spaces-btn" data-capy-action="reloadSpaces">← Back to spaces</button>' +
      '<h3>Widgets for '+escapeHtml(spaceId)+'</h3>' +
      '<div class="capy-spaces-muted">Safe metadata view. Generated widget code is intentionally not fetched or executed in this list.</div>' +
      '<div class="capy-spaces-widget-list">'+widgetCards+'</div>' +
      '<div class="capy-spaces-form" aria-label="Add or update widget">' +
      '<label>Widget ID<input id="capyWidgetId" type="text" autocomplete="off" placeholder="weather"></label>' +
      '<label>Title<input id="capyWidgetTitle" type="text" autocomplete="off" placeholder="Weather"></label>' +
      '<label>Kind<input id="capyWidgetKind" type="text" autocomplete="off" value="markdown"></label>' +
      '<label>X<input id="capyWidgetX" type="number" min="0" step="1" value="0"></label>' +
      '<label>Y<input id="capyWidgetY" type="number" min="0" step="1" value="0"></label>' +
      '<label>W<input id="capyWidgetW" type="number" min="1" max="24" step="1" value="6"></label>' +
      '<label>H<input id="capyWidgetH" type="number" min="1" max="24" step="1" value="4"></label>' +
      '<button type="button" class="capy-spaces-btn" data-capy-action="saveWidget" data-space-id="'+escapeHtml(spaceId)+'">Save widget</button>' +
      '</div></div>';
  }

  function layoutNumber(value, fallback, min, max){
    const parsed = parseInt(value, 10);
    const n = Number.isFinite(parsed) ? parsed : fallback;
    return Math.max(min, Math.min(max, n));
  }

  function widgetLayout(widget){
    const raw = widget && widget.layout && typeof widget.layout === 'object' ? widget.layout : {};
    return {
      x: layoutNumber(raw.x, 0, 0, 10000),
      y: layoutNumber(raw.y, 0, 0, 10000),
      w: layoutNumber(raw.w, 6, 1, 24),
      h: layoutNumber(raw.h, 4, 1, 24),
    };
  }

  function formLayout(root){
    return {
      x: layoutNumber((getRootInput(root, '#capyWidgetX') || {}).value, 0, 0, 10000),
      y: layoutNumber((getRootInput(root, '#capyWidgetY') || {}).value, 0, 0, 10000),
      w: layoutNumber((getRootInput(root, '#capyWidgetW') || {}).value, 6, 1, 24),
      h: layoutNumber((getRootInput(root, '#capyWidgetH') || {}).value, 4, 1, 24),
    };
  }

  function formatWidgetLayout(layout){
    return 'x'+layout.x+' y'+layout.y+' · '+layout.w+'×'+layout.h;
  }

  function getRootInput(root, selector){
    return root && root.querySelector ? root.querySelector(selector) : null;
  }

  function currentSessionId(){
    try {
      return (typeof S !== 'undefined' && S.session && S.session.session_id) ? String(S.session.session_id) : '';
    } catch (err) {
      return '';
    }
  }

  function currentActiveSpaceId(){
    try {
      return (typeof S !== 'undefined' && S.session && S.session.active_space_id) ? String(S.session.active_space_id) : '';
    } catch (err) {
      return '';
    }
  }

  function setSpaceForm(root, spaceId, name, description){
    const idInput = getRootInput(root, '#capySpaceId');
    const nameInput = getRootInput(root, '#capySpaceName');
    const descriptionInput = getRootInput(root, '#capySpaceDescription');
    if (idInput) idInput.value = spaceId || '';
    if (nameInput) nameInput.value = name || '';
    if (descriptionInput) descriptionInput.value = description || '';
  }

  function setWidgetForm(root, widgetId, title, kind, layout){
    const idInput = getRootInput(root, '#capyWidgetId');
    const titleInput = getRootInput(root, '#capyWidgetTitle');
    const kindInput = getRootInput(root, '#capyWidgetKind');
    const xInput = getRootInput(root, '#capyWidgetX');
    const yInput = getRootInput(root, '#capyWidgetY');
    const wInput = getRootInput(root, '#capyWidgetW');
    const hInput = getRootInput(root, '#capyWidgetH');
    const safeLayout = widgetLayout({layout: layout || {}});
    if (idInput) idInput.value = widgetId || '';
    if (titleInput) titleInput.value = title || '';
    if (kindInput) kindInput.value = kind || 'markdown';
    if (xInput) xInput.value = String(safeLayout.x);
    if (yInput) yInput.value = String(safeLayout.y);
    if (wInput) wInput.value = String(safeLayout.w);
    if (hInput) hInput.value = String(safeLayout.h);
  }

  async function handleCapySpacesClick(event){
    const button = event.target && event.target.closest ? event.target.closest('[data-capy-action]') : null;
    if (!button) return;
    const action = button.dataset.capyAction;
    const spaceId = button.dataset.spaceId || '';
    if (action === 'loadWidgets') {
      await loadSpaceWidgets(spaceId);
      return;
    }
    if (action === 'openSpace') {
      await openSpaceDetail(spaceId);
      return;
    }
    if (action === 'activateSpace') {
      const sessionId = currentSessionId();
      if (!sessionId) return;
      const data = await postSpacesJson('api/spaces/activate', {space_id: spaceId, session_id: sessionId});
      if (data && data.session && typeof S !== 'undefined') S.session = data.session;
      await loadCapySpaces();
      return;
    }
    if (action === 'reloadSpaces') {
      await loadCapySpaces();
      return;
    }
    if (action === 'newSpace') {
      const root = document.getElementById('capySpacesRoot');
      if (root) root.dataset.editingSpaceId = '';
      setSpaceForm(root, '', '', '');
      return;
    }
    if (action === 'editSpace') {
      const root = document.getElementById('capySpacesRoot');
      if (root) root.dataset.editingSpaceId = spaceId;
      setSpaceForm(root, spaceId, button.dataset.spaceName || '', button.dataset.spaceDescription || '');
      return;
    }
    if (action === 'saveSpace') {
      const root = document.getElementById('capySpacesRoot');
      const idInput = getRootInput(root, '#capySpaceId');
      const nameInput = getRootInput(root, '#capySpaceName');
      const descriptionInput = getRootInput(root, '#capySpaceDescription');
      const editingSpaceId = root && root.dataset ? String(root.dataset.editingSpaceId || '').trim() : '';
      const name = nameInput ? nameInput.value : '';
      const description = descriptionInput ? descriptionInput.value : '';
      if (editingSpaceId) {
        await postSpacesJson('api/spaces/update', {space_id: editingSpaceId, updates: {name: name, description: description}});
      } else {
        await postSpacesJson('api/spaces/create', {space_id: idInput ? idInput.value : '', name: name, description: description});
      }
      await loadCapySpaces();
      return;
    }
    if (action === 'deleteSpace') {
      if (typeof showConfirmDialog !== 'function') return;
      const ok = await showConfirmDialog({title: 'Delete Capy Space?', message: 'Delete space "'+spaceId+'"? This removes its manifest and widgets.', confirmLabel: 'Delete', danger: true, focusCancel: true});
      if (!ok) return;
      await postSpacesJson('api/spaces/delete', {space_id: spaceId});
      await loadCapySpaces();
      return;
    }
    if (action === 'editWidget') {
      const root = document.getElementById('capySpacesRoot');
      setWidgetForm(root, button.dataset.widgetId || '', button.dataset.widgetTitle || '', button.dataset.widgetKind || 'markdown', {
        x: button.dataset.widgetX,
        y: button.dataset.widgetY,
        w: button.dataset.widgetW,
        h: button.dataset.widgetH,
      });
      return;
    }
    if (action === 'askWidget') {
      if (typeof showPromptDialog !== 'function') return;
      const widgetId = button.dataset.widgetId || '';
      const widgetTitle = button.dataset.widgetTitle || widgetId;
      const promptText = await showPromptDialog({
        title: 'Ask Capy about this widget',
        placeholder: 'Describe what Capy should do with '+widgetTitle,
        confirmLabel: 'Queue event',
      });
      if (!promptText) return;
      await postSpacesJson('api/spaces/widget/event', {
        space_id: spaceId,
        widget_id: widgetId,
        event_name: 'agent.prompt',
        prompt: promptText,
        payload: {source: 'widget-manager', widget_title: widgetTitle},
      });
      await loadSpaceWidgets(spaceId);
      return;
    }
    if (action === 'saveWidget') {
      const root = document.getElementById('capySpacesRoot');
      const idInput = getRootInput(root, '#capyWidgetId');
      const titleInput = getRootInput(root, '#capyWidgetTitle');
      const kindInput = getRootInput(root, '#capyWidgetKind');
      const widget = {
        id: idInput ? idInput.value : '',
        title: titleInput ? titleInput.value : '',
        kind: kindInput && kindInput.value ? kindInput.value : 'markdown',
        layout: formLayout(root),
      };
      await postSpacesJson('api/spaces/widget/upsert', {space_id: spaceId, widget: widget});
      await loadSpaceWidgets(spaceId);
      return;
    }
    if (action === 'deleteWidget') {
      await postSpacesJson('api/spaces/widget/delete', {space_id: spaceId, widget_id: button.dataset.widgetId || ''});
      await loadSpaceWidgets(spaceId);
    }
  }

  function ensureCapySpacesHandlers(){
    if (handlersBound) return;
    const root = document.getElementById('capySpacesRoot');
    if (!root || !root.addEventListener) return;
    root.addEventListener('click', handleCapySpacesClick);
    handlersBound = true;
  }

  function renderRecoverySnapshot(data){
    if (!data || !data.enabled) {
      return '<div class="capy-spaces-card"><h3>Capy Spaces recovery disabled</h3><div class="capy-spaces-muted">Capy Spaces recovery is disabled because Spaces are disabled.</div></div>';
    }
    const spaces = Array.isArray(data.spaces) ? data.spaces : [];
    const rows = spaces.length ? spaces.map(function(s){
      const spaceId = s.space_id || '';
      const name = s.name || spaceId || 'Untitled';
      const description = s.description || '';
      return '<div class="capy-spaces-widget" data-space-id="'+escapeHtml(spaceId)+'"><div><strong>'+escapeHtml(name)+'</strong>' +
        (description ? '<div class="capy-spaces-muted">'+escapeHtml(description)+'</div>' : '') +
        '<div class="capy-spaces-muted">Space ID: '+escapeHtml(spaceId)+' · Widgets: '+Number(s.widget_count||0)+' · Revision: '+escapeHtml(s.revision_event_id||'none')+'</div></div></div>';
    }).join('') : '<div class="capy-spaces-muted">No spaces found in recovery metadata.</div>';
    return '<div class="capy-spaces-card"><h3>Safe recovery</h3>' +
      '<div class="capy-spaces-muted">Generated widgets rendered: '+String(!!data.generated_widgets_rendered)+'. This panel lists metadata only so broken generated UI cannot execute here.</div>' +
      '<div class="capy-spaces-widget-list">'+rows+'</div></div>';
  }

  async function loadCapySpacesRecovery(){
    const root = document.getElementById('capySpacesRecovery');
    if (!root) return;
    try {
      const data = await fetchSpacesJson('api/spaces/recovery');
      root.innerHTML = renderRecoverySnapshot(data);
    } catch (err) {
      root.textContent = 'Safe recovery unavailable: '+(err.message||String(err));
    }
  }

  function escapeHtml(value){
    return String(value).replace(/[&<>'"]/g, function(ch){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'})[ch];
    });
  }

  window.loadCapySpaces = loadCapySpaces;
  window.loadCapySpacesRecovery = loadCapySpacesRecovery;
  window.loadSpaceWidgets = loadSpaceWidgets;
  window.openSpaceDetail = openSpaceDetail;
  window.addEventListener('DOMContentLoaded', function(){
    ensureCapySpacesHandlers();
    loadCapySpaces();
    loadCapySpacesRecovery();
  });
})();
