// Capy Spaces foundation shell.
// This UI exposes safe metadata and widget management without executing widget renderers.
(function(){
  var handlersBound = false;
  var recoveryHandlersBound = false;

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
    return '<div class="capy-spaces-card"><h3>Capy Spaces</h3><div class="capy-spaces-muted">'+spaces.length+' space(s). Widget management lists metadata only; generated widget code is not executed here.</div>' +
      '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn" data-capy-action="installWeatherTemplate">Install weather demo</button><button type="button" class="capy-spaces-btn" data-capy-action="installResearchTemplate">Install research harness</button><button type="button" class="capy-spaces-btn" data-capy-action="installDashboardTemplate">Install dashboard demo</button><button type="button" class="capy-spaces-btn" data-capy-action="installKanbanTemplate">Install kanban board</button><button type="button" class="capy-spaces-btn" data-capy-action="installNotesTemplate">Install notes app</button><button type="button" class="capy-spaces-btn" data-capy-action="installBrowserTemplate">Install browser surface</button><button type="button" class="capy-spaces-btn" data-capy-action="installStockTemplate">Install stock chart</button><button type="button" class="capy-spaces-btn" data-capy-action="installBigBangTemplate">Install Big Bang onboarding</button><button type="button" class="capy-spaces-btn" data-capy-action="reloadSpaces">Refresh</button><button type="button" class="capy-spaces-btn" data-capy-action="newSpace">New space</button></div></div>' +
      renderTrustedSystemWidgets() + cards + renderSpaceAgentImportForm() + renderSpaceForm();
  }

  function renderTrustedSystemWidgets(){
    const widgets = [
      {id: 'system.chat', panel: 'chat', title: 'Chat', description: 'Conversation surface for the active Capy Space.'},
      {id: 'system.workspaces', panel: 'workspaces', title: 'Spaces', description: 'Trusted workspace/files panel outside generated widgets.'},
      {id: 'system.tasks', panel: 'tasks', title: 'Tasks', description: 'Scheduled jobs and run status.'},
      {id: 'system.memory', panel: 'memory', title: 'Memory', description: 'Durable memory and recall controls.'},
      {id: 'system.settings', panel: 'settings', title: 'Settings', description: 'Provider, profile, sensitive configuration, and recovery controls stay in the trusted shell.'},
    ];
    const cards = widgets.map(w => '<div class="capy-spaces-system-widget" data-system-widget-id="'+escapeHtml(w.id)+'">' +
      '<div><strong>'+escapeHtml(w.title)+'</strong><div class="capy-spaces-muted">'+escapeHtml(w.id)+' · trusted WebUI system widget</div><div class="capy-spaces-muted">'+escapeHtml(w.description)+'</div></div>' +
      '<button type="button" class="capy-spaces-btn" data-capy-action="openSystemPanel" data-system-panel="'+escapeHtml(w.panel)+'">Open '+escapeHtml(w.title)+'</button>' +
      '</div>').join('');
    return '<div class="capy-spaces-card capy-spaces-system-shell"><h3>Trusted WebUI system widgets</h3>' +
      '<div class="capy-spaces-muted">Capy Spaces is now the workspace layer. These first-party panels are addressable as system.* widgets, while the auth/settings/recovery shell remains outside generated Space content.</div>' +
      '<div class="capy-spaces-system-grid">'+cards+'</div></div>';
  }

  function renderSpaceAgentImportForm(){
    return '<div class="capy-spaces-card"><h3>Import Space Agent YAML</h3>' +
      '<div class="capy-spaces-muted">Paste a Space Agent space.yaml and optional widgets JSON map. Imported generated sources are quarantined by the backend; this UI only renders safe metadata.</div>' +
      '<div class="capy-spaces-form" aria-label="Import Space Agent YAML package">' +
      '<label>space.yaml<textarea id="capySpaceAgentImportSpaceYaml" rows="5" autocomplete="off" placeholder="Paste Space Agent space metadata here"></textarea></label>' +
      '<label>Widgets JSON map<textarea id="capySpaceAgentImportWidgetsJson" rows="5" autocomplete="off" placeholder="Paste optional widget YAML map as JSON"></textarea></label>' +
      '<button type="button" class="capy-spaces-btn" data-capy-action="importSpaceAgentYaml">Import YAML package</button>' +
      '</div></div>';
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
      root.dataset.editingWidgetId = '';
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
      '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn" data-capy-action="activateSpace" data-space-id="'+escapeHtml(spaceId)+'">Use in chat</button><button type="button" class="capy-spaces-btn" data-capy-action="loadWidgets" data-space-id="'+escapeHtml(spaceId)+'">Manage widgets</button><button type="button" class="capy-spaces-btn" data-capy-action="exportSpaceYaml" data-space-id="'+escapeHtml(spaceId)+'">Export YAML</button><button type="button" class="capy-spaces-btn" data-capy-action="exportSpaceZip" data-space-id="'+escapeHtml(spaceId)+'">Export ZIP</button></div>' +
      '</div><div class="capy-spaces-card"><h3>Widgets</h3><div class="capy-spaces-muted">Metadata-only detail view. Generated widget code is intentionally not displayed or executed.</div><div class="capy-spaces-widget-list">'+widgetRows+'</div></div>' +
      renderRevisionHistory(revisions || []);
  }

  function renderSpaceExportResult(spaceId, data){
    const rawFormat = data && data.format ? String(data.format) : 'yaml';
    const format = rawFormat.indexOf('zip') >= 0 ? 'zip' : 'yaml';
    const filename = data && data.filename ? String(data.filename) : String(spaceId || 'space') + '-space-agent.' + (format === 'zip' ? 'zip' : 'yaml');
    const widgetCount = data && Number.isFinite(Number(data.widget_count)) ? Number(data.widget_count) : 0;
    return '<div class="capy-spaces-card"><h3>Space Agent export ready</h3>' +
      '<div class="capy-spaces-muted">Safe metadata package generated. Package contents are intentionally not displayed in this UI.</div>' +
      '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div><strong>'+escapeHtml(filename)+'</strong>' +
      '<div class="capy-spaces-muted">Format: '+escapeHtml(format)+' · Space ID: '+escapeHtml(spaceId || '')+' · Widgets: '+widgetCount+'</div></div></div></div></div>';
  }

  function renderSpaceImportResult(data){
    const space = data && data.space && typeof data.space === 'object' ? data.space : {};
    const spaceId = space.space_id || data && data.space_id || '';
    const name = space.name || spaceId || 'Imported space';
    const widgets = Array.isArray(data && data.imported_widgets) ? data.imported_widgets : [];
    const widgetRows = widgets.length ? widgets.map(function(w){
      const widgetId = w && w.id ? String(w.id) : '';
      const title = w && w.title ? String(w.title) : widgetId || 'Untitled widget';
      const kind = w && w.kind ? String(w.kind) : (w && w.type ? String(w.type) : 'custom');
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(title)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(kind)+' · '+escapeHtml(widgetId)+'</div></div></div>';
    }).join('') : '<div class="capy-spaces-muted">No imported widget metadata returned.</div>';
    const count = widgets.length;
    return '<div class="capy-spaces-card"><h3>Space Agent import ready</h3>' +
      '<div class="capy-spaces-muted">Imported package metadata only. Generated widget sources remain quarantined/disabled for review by the backend.</div>' +
      '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div><strong>'+escapeHtml(name)+'</strong>' +
      '<div class="capy-spaces-muted">Space ID: '+escapeHtml(spaceId)+' · '+count+' widget'+(count === 1 ? '' : 's')+'</div></div></div>'+widgetRows+'</div></div>';
  }

  function renderSpaceImportError(message){
    return '<div class="capy-spaces-card"><h3>Space Agent import blocked</h3>' +
      '<div class="capy-spaces-muted">'+escapeHtml(message || 'Import payload could not be parsed safely.')+'</div></div>';
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
    if (root && root.dataset) root.dataset.editingWidgetId = widgetId || '';
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
    if (action === 'openSystemPanel') {
      const panel = button.dataset.systemPanel || '';
      if (['chat', 'workspaces', 'tasks', 'memory', 'settings'].indexOf(panel) === -1) return;
      if (typeof switchPanel === 'function') await switchPanel(panel);
      return;
    }
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
    if (action === 'exportSpaceYaml' || action === 'exportSpaceZip') {
      const format = action === 'exportSpaceZip' ? 'zip' : 'yaml';
      const data = await postSpacesJson('api/spaces/export', {space_id: spaceId, format: format});
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderSpaceExportResult(spaceId, data || {}) + root.innerHTML;
      return;
    }
    if (action === 'importSpaceAgentYaml') {
      const root = document.getElementById('capySpacesRoot');
      const spaceYamlInput = getRootInput(root, '#capySpaceAgentImportSpaceYaml');
      const widgetsInput = getRootInput(root, '#capySpaceAgentImportWidgetsJson');
      let widgets = {};
      const widgetsText = widgetsInput && widgetsInput.value ? String(widgetsInput.value).trim() : '';
      if (widgetsText) {
        try {
          widgets = JSON.parse(widgetsText);
        } catch (err) {
          if (root) root.innerHTML = renderSpaceImportError('Widgets JSON map is invalid; no import request was sent.') + root.innerHTML;
          return;
        }
      }
      const data = await postSpacesJson('api/spaces/import', {space_yaml: spaceYamlInput ? spaceYamlInput.value : '', widgets: widgets});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) refreshedRoot.innerHTML = renderSpaceImportResult(data || {}) + refreshedRoot.innerHTML;
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
    if (action === 'installWeatherTemplate') {
      await postSpacesJson('api/spaces/templates/install', {template: 'weather'});
      await loadCapySpaces();
      return;
    }
    if (action === 'installResearchTemplate') {
      await postSpacesJson('api/spaces/templates/install', {template: 'research'});
      await loadCapySpaces();
      return;
    }
    if (action === 'installDashboardTemplate') {
      await postSpacesJson('api/spaces/templates/install', {template: 'dashboard'});
      await loadCapySpaces();
      return;
    }
    if (action === 'installKanbanTemplate') {
      await postSpacesJson('api/spaces/templates/install', {template: 'kanban'});
      await loadCapySpaces();
      return;
    }
    if (action === 'installNotesTemplate') {
      await postSpacesJson('api/spaces/templates/install', {template: 'notes'});
      await loadCapySpaces();
      return;
    }
    if (action === 'installBrowserTemplate') {
      await postSpacesJson('api/spaces/templates/install', {template: 'browser'});
      await loadCapySpaces();
      return;
    }
    if (action === 'installStockTemplate') {
      await postSpacesJson('api/spaces/templates/install', {template: 'stock'});
      await loadCapySpaces();
      return;
    }
    if (action === 'installBigBangTemplate') {
      await postSpacesJson('api/spaces/templates/install', {template: 'big-bang'});
      await loadCapySpaces();
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
      const editingWidgetId = root && root.dataset ? String(root.dataset.editingWidgetId || '').trim() : '';
      if (editingWidgetId) {
        await postSpacesJson('api/spaces/widget/patch', {space_id: spaceId, widget_id: editingWidgetId, patch: {title: widget.title, kind: widget.kind, layout: widget.layout}});
      } else {
        await postSpacesJson('api/spaces/widget/upsert', {space_id: spaceId, widget: widget});
      }
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
      const widgets = Array.isArray(s.widgets) ? s.widgets : [];
      const widgetRows = widgets.length ? '<div class="capy-spaces-widget-list">'+widgets.map(function(w){
        const widgetId = w && w.id ? String(w.id) : '';
        const title = w && w.title ? String(w.title) : widgetId || 'Untitled widget';
        const kind = w && w.kind ? String(w.kind) : 'custom';
        const disabled = !!(w && w.disabled);
        const disabledReason = w && w.disabled_reason ? String(w.disabled_reason) : '';
        return '<div class="capy-spaces-widget" data-widget-id="'+escapeHtml(widgetId)+'"><div><strong>'+escapeHtml(title)+'</strong>' +
          '<div class="capy-spaces-muted">'+escapeHtml(kind)+' · '+escapeHtml(widgetId)+(disabled ? ' · Disabled'+(disabledReason ? ': '+escapeHtml(disabledReason) : '') : '')+'</div></div>' +
          '<div class="capy-spaces-actions">' +
          (disabled ? '<span class="capy-spaces-muted">Disabled</span>' : '<button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="disableRecoveryWidget" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'">Disable widget</button>') +
          '</div></div>';
      }).join('')+'</div>' : '<div class="capy-spaces-muted">No widget metadata available for this space.</div>';
      return '<div class="capy-spaces-widget" data-space-id="'+escapeHtml(spaceId)+'"><div><strong>'+escapeHtml(name)+'</strong>' +
        (description ? '<div class="capy-spaces-muted">'+escapeHtml(description)+'</div>' : '') +
        '<div class="capy-spaces-muted">Space ID: '+escapeHtml(spaceId)+' · Widgets: '+Number(s.widget_count||0)+' · Revision: '+escapeHtml(s.revision_event_id||'none')+'</div>' +
        widgetRows + '</div></div>';
    }).join('') : '<div class="capy-spaces-muted">No spaces found in recovery metadata.</div>';
    return '<div class="capy-spaces-card"><h3>Safe recovery</h3>' +
      '<div class="capy-spaces-muted">Generated widgets rendered: '+String(!!data.generated_widgets_rendered)+'. This panel lists metadata only so broken generated UI cannot execute here.</div>' +
      '<div class="capy-spaces-widget-list">'+rows+'</div></div>';
  }

  async function handleCapySpacesRecoveryClick(event){
    const button = event.target && event.target.closest ? event.target.closest('[data-capy-action]') : null;
    if (!button) return;
    const action = button.dataset.capyAction;
    if (action !== 'disableRecoveryWidget') return;
    if (typeof showConfirmDialog !== 'function') return;
    const spaceId = button.dataset.spaceId || '';
    const widgetId = button.dataset.widgetId || '';
    if (!spaceId || !widgetId) return;
    const ok = await showConfirmDialog({title: 'Disable widget?', message: 'Disable widget "'+widgetId+'" from safe recovery? The source is preserved for repair/rollback.', confirmLabel: 'Disable widget', danger: true, focusCancel: true});
    if (!ok) return;
    await postSpacesJson('api/spaces/recovery/disable-widget', {space_id: spaceId, widget_id: widgetId, reason: 'disabled from recovery panel'});
    await loadCapySpacesRecovery();
  }

  function ensureCapySpacesRecoveryHandlers(){
    if (recoveryHandlersBound) return;
    const root = document.getElementById('capySpacesRecovery');
    if (!root || !root.addEventListener) return;
    root.addEventListener('click', handleCapySpacesRecoveryClick);
    recoveryHandlersBound = true;
  }

  async function loadCapySpacesRecovery(){
    const root = document.getElementById('capySpacesRecovery');
    if (!root) return;
    ensureCapySpacesRecoveryHandlers();
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
