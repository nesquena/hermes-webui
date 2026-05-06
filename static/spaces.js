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
      let demoData = {demos: []};
      try {
        demoData = await fetchSpacesJson('api/spaces/demo/runs');
      } catch (demoErr) {
        demoData = {demos: []};
      }
      const data = await fetchSpacesJson('api/spaces');
      if (!data.enabled) {
        root.innerHTML = '<div class="capy-spaces-card"><h3>Capy Spaces disabled</h3><div class="capy-spaces-muted">Set HERMES_WEBUI_SPACES_ENABLED=1 to enable the foundation shell.</div></div>';
        return;
      }
      root.dataset.editingSpaceId = '';
      const spaces = data.spaces || [];
      root.innerHTML = renderSpacesList(spaces, demoData.demos || []);
    } catch (err) {
      root.innerHTML = '<div class="capy-spaces-card"><h3>Capy Spaces unavailable</h3><div class="capy-spaces-muted">'+escapeHtml(err.message||String(err))+'</div></div>';
    }
  }

  function renderTemplateInstallStatus(result){
    const template = String(result && result.template || '').trim().toLowerCase();
    const space = result && result.space && typeof result.space === 'object' ? result.space : {};
    const widgets = Array.isArray(result && result.installed_widgets) ? result.installed_widgets : [];
    const spaceId = space.space_id ? String(space.space_id) : '';
    const spaceName = space.name || spaceId || 'Demo Space';
    const widgetCount = widgets.length || Number(space.widget_count || 0);
    const widgetLabel = widgetCount === 1 ? '1 widget' : widgetCount+' widgets';
    const title = template === 'weather' ? 'Weather demo installed' : (template === 'notes' ? 'Notes app installed' : (template === 'kanban' ? 'Kanban board installed' : (template === 'research' ? 'Research harness installed' : (template === 'dashboard' ? 'Dashboard demo installed' : (template === 'camera' ? 'Camera dashboard installed' : (template === 'browser' ? 'Browser surface installed' : (template === 'stock' ? 'Stock chart installed' : (template === 'service' ? 'Local service dashboard installed' : 'Template installed'))))))));
    const openLabel = template === 'weather' ? 'Open weather demo' : (template === 'notes' ? 'Open notes app' : (template === 'kanban' ? 'Open kanban board' : (template === 'research' ? 'Open research harness' : (template === 'dashboard' ? 'Open dashboard demo' : (template === 'camera' ? 'Open camera dashboard' : (template === 'browser' ? 'Open browser surface' : (template === 'stock' ? 'Open stock chart' : (template === 'service' ? 'Open local service dashboard' : 'Open Space'))))))));
    const manageLabel = template === 'weather' ? 'Manage weather widget' : (template === 'notes' ? 'Manage notes widgets' : (template === 'kanban' ? 'Manage kanban widgets' : (template === 'research' ? 'Manage research widgets' : (template === 'dashboard' ? 'Manage dashboard widgets' : (template === 'camera' ? 'Manage camera widgets' : (template === 'browser' ? 'Manage browser widgets' : (template === 'stock' ? 'Manage stock widgets' : (template === 'service' ? 'Manage service widgets' : 'Manage widgets'))))))));
    const widgetItems = widgets.slice(0, 6).map(function(w){
      return '<li>'+escapeHtml(w.title || w.id || 'Widget')+'</li>';
    }).join('');
    const smokeAction = template === 'weather'
      ? '<button type="button" class="capy-spaces-btn" data-capy-action="runDemoSmoke" data-demo="demo_weather_widget">Run weather smoke</button>'
      : (template === 'notes' ? '<button type="button" class="capy-spaces-btn" data-capy-action="runDemoSmoke" data-demo="demo_notes_app">Run notes smoke</button>' : (template === 'kanban' ? '<button type="button" class="capy-spaces-btn" data-capy-action="runDemoSmoke" data-demo="demo_kanban_board">Run kanban smoke</button>' : ''));
    const actions = spaceId ? '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn" data-capy-action="openSpace" data-space-id="'+escapeHtml(spaceId)+'">'+escapeHtml(openLabel)+'</button><button type="button" class="capy-spaces-btn" data-capy-action="loadWidgets" data-space-id="'+escapeHtml(spaceId)+'">'+escapeHtml(manageLabel)+'</button>'+smokeAction+'</div>' : '';
    return '<div class="capy-spaces-card" role="status"><h3>'+escapeHtml(title)+'</h3>' +
      '<div class="capy-spaces-muted">'+escapeHtml(spaceName)+' · '+escapeHtml(widgetLabel)+' · safe metadata-only install</div>' +
      (widgetItems ? '<ul>'+widgetItems+'</ul>' : '') + actions +
      '</div>';
  }

  function renderTemplateResetStatus(result){
    const space = result && result.space ? result.space : {};
    const widgets = Array.isArray(result && result.installed_widgets) ? result.installed_widgets : [];
    const widgetItems = widgets.map(function(w){
      return '<li>'+escapeHtml(w.title || w.id || 'Widget')+'</li>';
    }).join('');
    const widgetCount = widgets.length;
    const widgetLabel = widgetCount === 1 ? '1 widget' : widgetCount+' widgets';
    return '<div class="capy-spaces-card" role="status"><h3>Big Bang onboarding reset</h3>' +
      '<div class="capy-spaces-muted">'+escapeHtml(space.name || 'Big Bang Onboarding')+' restored to safe canonical metadata · '+escapeHtml(widgetLabel)+'</div>' +
      (widgetItems ? '<ul>'+widgetItems+'</ul>' : '') +
      '</div>';
  }

  function renderWidgetEventQueuedStatus(result){
    const eventName = String(result && result.event_name || 'widget.event').trim();
    const widgetId = String(result && result.widget_id || '').trim();
    const eventId = String(result && result.event_id || '').trim();
    const title = eventName === 'widget.refresh' ? 'Weather refresh queued' : (eventName === 'agent.prompt' ? 'Weather prompt queued' : 'Widget event queued');
    const meta = [widgetId, eventName, eventId].filter(Boolean).join(' · ');
    return '<div class="capy-spaces-card" role="status"><h3>'+escapeHtml(title)+'</h3>' +
      '<div class="capy-spaces-muted">'+escapeHtml(meta || 'Metadata-only event queued')+'</div>' +
      '<div class="capy-spaces-muted">Prompt bodies and generated widget code stay redacted.</div></div>';
  }

  function renderSpacesList(spaces, demos){
    const activeSpaceId = currentActiveSpaceId();
    const cards = spaces.length ? spaces.map(function(s){
      const spaceId = s.space_id || '';
      const name = s.name || spaceId || 'Untitled';
      const description = s.description || '';
      const activeLabel = activeSpaceId && activeSpaceId === spaceId ? ' · Active in chat' : '';
      const activeAction = activeSpaceId && activeSpaceId === spaceId
        ? '<button type="button" class="capy-spaces-btn" data-capy-action="clearActiveSpace">Clear from chat</button>'
        : '<button type="button" class="capy-spaces-btn" data-capy-action="activateSpace" data-space-id="'+escapeHtml(spaceId)+'">Use in chat</button>';
      const widgetCount = Number(s.widget_count||0);
      const widgetLabel = widgetCount === 1 ? '1 widget' : widgetCount+' widgets';
      const resetAction = spaceId === 'big-bang-onboarding'
        ? '<button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="resetBigBangTemplate" data-space-id="'+escapeHtml(spaceId)+'">Reset Big Bang onboarding</button>'
        : '';
      return '<div class="capy-spaces-card" data-space-id="'+escapeHtml(spaceId)+'">' +
        '<div class="capy-spaces-card-row"><div><strong>'+escapeHtml(name)+'</strong>' +
        (description ? '<div class="capy-spaces-muted">'+escapeHtml(description)+'</div>' : '') +
        '<div class="capy-spaces-muted">'+escapeHtml(widgetLabel)+' · Revision: '+escapeHtml(s.revision_event_id||'none')+escapeHtml(activeLabel)+'</div></div>' +
        '<div class="capy-spaces-actions">' +
        '<button type="button" class="capy-spaces-btn" data-capy-action="openSpace" data-space-id="'+escapeHtml(spaceId)+'">Open</button>' +
        activeAction +
        '<button type="button" class="capy-spaces-btn" data-capy-action="editSpace" data-space-id="'+escapeHtml(spaceId)+'" data-space-name="'+escapeHtml(name)+'" data-space-description="'+escapeHtml(description)+'">Edit</button>' +
        '<button type="button" class="capy-spaces-btn" data-capy-action="loadWidgets" data-space-id="'+escapeHtml(spaceId)+'">Manage widgets</button>' +
        resetAction +
        '<button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="deleteSpace" data-space-id="'+escapeHtml(spaceId)+'">Delete</button>' +
        '</div></div>' +
        '</div>';
    }).join('') : '<div class="capy-spaces-card"><strong>No spaces yet</strong><div class="capy-spaces-muted">Create a space below to start adding safe metadata-only widgets.</div></div>';
    return '<div class="capy-spaces-card"><h3>Capy Spaces</h3><div class="capy-spaces-muted">'+spaces.length+' space(s). Widget management lists metadata only; generated widget code is not executed here.</div>' +
      '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn" data-capy-action="createSpaceFromSession">Create from current chat</button><button type="button" class="capy-spaces-btn" data-capy-action="installWeatherTemplate">Install weather demo</button><button type="button" class="capy-spaces-btn" data-capy-action="installResearchTemplate">Install research harness</button><button type="button" class="capy-spaces-btn" data-capy-action="installDashboardTemplate">Install dashboard demo</button><button type="button" class="capy-spaces-btn" data-capy-action="installCameraTemplate">Install camera dashboard</button><button type="button" class="capy-spaces-btn" data-capy-action="installKanbanTemplate">Install kanban board</button><button type="button" class="capy-spaces-btn" data-capy-action="installNotesTemplate">Install notes app</button><button type="button" class="capy-spaces-btn" data-capy-action="installBrowserTemplate">Install browser surface</button><button type="button" class="capy-spaces-btn" data-capy-action="installStockTemplate">Install stock chart</button><button type="button" class="capy-spaces-btn" data-capy-action="installServiceTemplate">Install local service dashboard</button><button type="button" class="capy-spaces-btn" data-capy-action="installModelSetupTemplate">Install model setup</button><button type="button" class="capy-spaces-btn" data-capy-action="installGameTemplate">Install game sandbox</button><button type="button" class="capy-spaces-btn" data-capy-action="installMusicTemplate">Install music sequencer</button><button type="button" class="capy-spaces-btn" data-capy-action="installBigBangTemplate">Install Big Bang onboarding</button><button type="button" class="capy-spaces-btn" data-capy-action="reloadSpaces">Refresh</button><button type="button" class="capy-spaces-btn" data-capy-action="newSpace">New space</button></div></div>' +
      renderDemoSmokeRunner(demos || []) + renderTrustedSystemWidgets(activeSpaceId) + cards + renderSpaceAgentImportForm() + renderSpaceForm();
  }

  function renderDemoSmokeRunner(demos){
    const safeDemos = Array.isArray(demos) ? demos : [];
    const rows = safeDemos.length ? safeDemos.slice(0, 20).map(function(d){
      const demo = d && d.demo ? String(d.demo) : '';
      const title = d && d.title ? String(d.title) : demo || 'Demo smoke';
      const template = d && d.template ? String(d.template) : 'unknown';
      const mode = d && d.mode ? String(d.mode) : 'metadata-only-smoke';
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(title)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(demo)+' · template: '+escapeHtml(template)+' · '+escapeHtml(mode)+'</div></div>' +
        '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn" data-capy-action="runDemoSmoke" data-demo="'+escapeHtml(demo)+'">Run smoke</button></div></div>';
    }).join('') : '<div class="capy-spaces-muted">No metadata-only demo smokes advertised by the backend.</div>';
    return '<div class="capy-spaces-card"><h3>Demo parity smoke runner</h3>' +
      '<div class="capy-spaces-muted">Runs safe Space Agent video-parity fixtures through typed Capy Space APIs only; no generated widget code is executed.</div>' +
      '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn" data-capy-action="runAllDemoSmokes">Run all smokes</button></div>' +
      '<div class="capy-spaces-widget-list">'+rows+'</div></div>';
  }

  function renderWeatherPromptFlow(flow){
    if (!flow || typeof flow !== 'object' || Array.isArray(flow)) return '';
    const blankSpace = flow.blank_space ? 'yes' : 'no';
    const widgetCreated = flow.widget_created ? 'created' : 'not created';
    const reloadVerified = flow.reload_verified ? 'verified' : 'not verified';
    const query = flow.query ? String(flow.query) : '';
    const chatAnswer = flow.chat_answer_status ? String(flow.chat_answer_status) : '';
    const answerPreview = flow.answer_preview ? String(flow.answer_preview) : '';
    const widgetRequest = flow.widget_request ? String(flow.widget_request) : '';
    const networkMode = flow.network_mode ? String(flow.network_mode) : '';
    return '<div class="capy-spaces-card capy-spaces-demo-flow"><h4>Prompt → widget flow</h4>' +
      '<div class="capy-spaces-muted">Blank space: '+escapeHtml(blankSpace)+' · Widget: '+escapeHtml(widgetCreated)+' · Widget after reload: '+escapeHtml(reloadVerified)+'</div>' +
      (query ? '<div class="capy-spaces-muted">Query: '+escapeHtml(query)+'</div>' : '') +
      (chatAnswer ? '<div class="capy-spaces-muted">Chat answer: '+escapeHtml(chatAnswer)+'</div>' : '') +
      (answerPreview ? '<div class="capy-spaces-muted">Answer preview: '+escapeHtml(answerPreview)+'</div>' : '') +
      (widgetRequest ? '<div class="capy-spaces-muted">Widget request: '+escapeHtml(widgetRequest)+'</div>' : '') +
      (networkMode ? '<div class="capy-spaces-muted">Network mode: '+escapeHtml(networkMode)+'</div>' : '') +
      '</div>';
  }

  function renderDemoSmokeResult(data){
    const space = data && data.space && typeof data.space === 'object' ? data.space : {};
    const demo = data && data.demo ? String(data.demo) : 'demo';
    const spaceName = space.name || space.space_id || 'Space demo';
    const widgetCount = Number(data && data.widget_count || 0);
    const persistedWidgetCount = Number(data && data.persisted_widget_count || 0);
    const persistence = data && data.persistence_checked ? 'checked' : 'not checked';
    const revisionCount = Number(data && data.revision_event_count || 0);
    const rollbackPoint = data && data.rollback_point ? 'yes' : 'no';
    const action = data && data.action ? String(data.action) : '';
    const queuedEventCount = Number(data && data.queued_event_count || 0);
    const rollbackCheck = data && data.research_rollback_check && typeof data.research_rollback_check === 'object'
      ? data.research_rollback_check
      : null;
    const extraParts = [];
    if (action) extraParts.push('Action: '+escapeHtml(action));
    if (queuedEventCount) extraParts.push('Queued events: '+queuedEventCount);
    if (rollbackCheck && rollbackCheck.verified === true) extraParts.push('Rollback verified: yes');
    const extraLine = extraParts.length ? '<div class="capy-spaces-muted">'+extraParts.join(' · ')+'</div>' : '';
    const weatherObservation = data && data.weather_observation && typeof data.weather_observation === 'object' && !Array.isArray(data.weather_observation)
      ? data.weather_observation
      : {};
    const weatherWidget = weatherObservation.widget && typeof weatherObservation.widget === 'object' && !Array.isArray(weatherObservation.widget)
      ? weatherObservation.widget
      : {};
    const weatherPreview = renderWeatherObservation(weatherWidget.metadata || {});
    const promptFlowPreview = weatherPreview ? renderWeatherPromptFlow(data && data.prompt_flow) : '';
    const notesArtifact = data && data.notes_artifact && typeof data.notes_artifact === 'object' && !Array.isArray(data.notes_artifact)
      ? data.notes_artifact
      : {};
    const notesPreview = renderNotesSmokePreview(notesArtifact);
    const kanbanBoard = data && data.kanban_board && typeof data.kanban_board === 'object' && !Array.isArray(data.kanban_board)
      ? data.kanban_board
      : {};
    const kanbanPreview = renderKanbanSmokePreview(kanbanBoard);
    const demoSpaceId = space.space_id ? String(space.space_id) : '';
    const hasNotesPreview = !!notesPreview;
    const hasKanbanPreview = !!kanbanPreview;
    const manageLabel = weatherPreview ? 'Manage weather widget' : (hasNotesPreview ? 'Manage notes widgets' : (hasKanbanPreview ? 'Manage kanban widgets' : 'Manage demo widgets'));
    const demoActions = demoSpaceId
      ? '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn" data-capy-action="openSpace" data-space-id="'+escapeHtml(demoSpaceId)+'">Open demo Space</button><button type="button" class="capy-spaces-btn" data-capy-action="loadWidgets" data-space-id="'+escapeHtml(demoSpaceId)+'">'+escapeHtml(manageLabel)+'</button></div>'
      : '';
    return '<div class="capy-spaces-card" role="status"><h3>Demo parity smoke passed</h3>' +
      '<div class="capy-spaces-muted">'+escapeHtml(demo)+' · '+escapeHtml(data && data.mode || 'metadata-only-smoke')+'</div>' +
      '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div><strong>'+escapeHtml(spaceName)+'</strong>' +
      '<div class="capy-spaces-muted">Space ID: '+escapeHtml(space.space_id || '')+' · Widgets: '+widgetCount+' · Persisted widgets: '+persistedWidgetCount+' · Persistence: '+escapeHtml(persistence)+' · Revisions: '+revisionCount+' · Rollback point: '+escapeHtml(rollbackPoint)+'</div>' +
      extraLine + '</div>'+demoActions+'</div></div>'+weatherPreview+promptFlowPreview+notesPreview+kanbanPreview+'</div>';
  }

  function renderDemoSmokeSuiteResult(data){
    const total = Number(data && data.total || 0);
    const passed = Number(data && data.passed || 0);
    const failed = Number(data && data.failed || 0);
    const results = Array.isArray(data && data.results) ? data.results : [];
    const rows = results.slice(0, 20).map(function(item){
      const demo = item && item.demo ? String(item.demo) : 'demo';
      const template = item && item.template ? String(item.template) : 'template';
      const widgetCount = Number(item && item.widget_count || 0);
      const persistedWidgetCount = Number(item && item.persisted_widget_count || 0);
      const persistence = item && item.persistence_checked ? 'checked' : 'not checked';
      const rollbackPoint = item && item.rollback_point ? 'yes' : 'no';
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(demo)+'</strong>' +
        '<div class="capy-spaces-muted">template: '+escapeHtml(template)+' · widgets: '+widgetCount+' · persisted: '+persistedWidgetCount+' · persistence: '+escapeHtml(persistence)+' · rollback point: '+escapeHtml(rollbackPoint)+'</div></div></div>';
    }).join('');
    return '<div class="capy-spaces-card" role="status"><h3>Demo parity smoke suite '+(failed ? 'finished' : 'passed')+'</h3>' +
      '<div class="capy-spaces-muted">'+passed+' / '+total+' metadata-only smokes passed</div>' +
      '<div class="capy-spaces-widget-list">'+rows+'</div></div>';
  }

  function renderTrustedSystemWidgets(activeSpaceId){
    const widgets = [
      {id: 'system.chat', panel: 'chat', title: 'Chat', description: 'Conversation surface for the active Capy Space.'},
      {id: 'system.workspaces', panel: 'workspaces', title: 'Spaces', description: 'Trusted workspace/files panel outside generated widgets.'},
      {id: 'system.tasks', panel: 'tasks', title: 'Tasks', description: 'Scheduled jobs and run status.'},
      {id: 'system.memory', panel: 'memory', title: 'Memory', description: 'Durable memory and recall controls.'},
      {id: 'system.settings', panel: 'settings', title: 'Settings', description: 'Provider, profile, sensitive configuration, and recovery controls stay in the trusted shell.'},
    ];
    const cards = widgets.map(w => {
      const addButton = activeSpaceId ? '<button type="button" class="capy-spaces-btn" data-capy-action="addSystemWidget" data-space-id="'+escapeHtml(activeSpaceId)+'" data-system-panel="'+escapeHtml(w.panel)+'">Add to active Space</button>' : '';
      return '<div class="capy-spaces-system-widget" data-system-widget-id="'+escapeHtml(w.id)+'">' +
        '<div><strong>'+escapeHtml(w.title)+'</strong><div class="capy-spaces-muted">'+escapeHtml(w.id)+' · trusted WebUI system widget</div><div class="capy-spaces-muted">'+escapeHtml(w.description)+'</div></div>' +
        '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn" data-capy-action="openSystemPanel" data-system-panel="'+escapeHtml(w.panel)+'">Open '+escapeHtml(w.title)+'</button>'+addButton+'</div>' +
        '</div>';
    }).join('');
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
      '</div></div>' +
      '<div class="capy-spaces-card"><h3>Import Space Agent ZIP</h3>' +
      '<div class="capy-spaces-muted">Paste a base64-encoded Space Agent ZIP. The archive is sent to the backend for bounded metadata-only import; archive contents are never rendered here.</div>' +
      '<div class="capy-spaces-form" aria-label="Import Space Agent ZIP package">' +
      '<label>ZIP archive base64<textarea id="capySpaceAgentImportZipB64" rows="4" autocomplete="off" placeholder="Paste base64 ZIP archive here"></textarea></label>' +
      '<button type="button" class="capy-spaces-btn" data-capy-action="importSpaceAgentZip">Import ZIP package</button>' +
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
      let eventsData = {events: []};
      try {
        eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(safeSpaceId));
      } catch (eventErr) {
        eventsData = {events: []};
      }
      const data = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(safeSpaceId));
      root.dataset.editingWidgetId = '';
      root.innerHTML = renderWidgetManager(safeSpaceId, data.widgets || [], eventsData.events || []);
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
      renderSharedDataSlots(spaceId, space.shared_data || []) +
      renderRevisionHistory(spaceId, revisions || []);
  }

  function renderSharedDataSlots(spaceId, slots){
    const safeSlots = Array.isArray(slots) ? slots.slice(0, 10) : [];
    if (!safeSlots.length) return '';
    const rows = safeSlots.map(function(slot){
      const key = safeSharedDataKey(slot && slot.key);
      if (!key) return '';
      const valueText = formatSharedDataSummary(slot && slot.value_summary);
      const metadataText = formatSharedDataSummary(slot && slot.metadata_summary);
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(key)+'</strong>' +
        (valueText ? '<div class="capy-spaces-muted">'+escapeHtml(valueText)+'</div>' : '') +
        (metadataText ? '<div class="capy-spaces-muted">Metadata: '+escapeHtml(metadataText)+'</div>' : '') +
        '</div><div class="capy-spaces-widget-actions"><button type="button" class="capy-spaces-btn" data-capy-action="deleteSharedData" data-space-id="'+escapeHtml(spaceId || '')+'" data-data-key="'+escapeHtml(key)+'">Delete data slot</button></div></div>';
    }).filter(Boolean).join('');
    if (!rows) return '';
    return '<div class="capy-spaces-card"><h3>Shared data</h3>' +
      '<div class="capy-spaces-muted">Metadata-only per-space data slots for widget cooperation. Raw values and secrets are not displayed.</div>' +
      '<div class="capy-spaces-widget-list">'+rows+'</div></div>';
  }

  function safeSharedDataKey(value){
    const text = String(value || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    if (!text) return '';
    const unsafeValuePattern = /(api[_-]?key|apikey|authorization|bearer|cookie|credential|credentials|password|secret|token|<script|<\/script|javascript:|onerror|onload|renderer|html|script|source)/i;
    return unsafeValuePattern.test(text) ? '' : text;
  }

  function formatSharedDataSummary(details){
    if (!details || typeof details !== 'object' || Array.isArray(details)) return '';
    const unsafeParts = ['renderer', 'html', 'script', 'data', 'source', 'api_key', 'apikey', 'token', 'password', 'secret', 'credential', 'credentials', 'cookie', 'authorization'];
    const unsafeValuePattern = /(api[_-]?key|apikey|authorization|bearer|cookie|credential|credentials|password|secret|token|<script|<\/script|javascript:|onerror|onload)/i;
    function keyIsSafe(key){
      const lowered = String(key || '').toLowerCase();
      if (lowered === 'source_widget') return true;
      return lowered && !unsafeParts.some(part => lowered.indexOf(part) >= 0);
    }
    function textValueSummary(value){
      const text = String(value == null ? '' : value).replace(/\s+/g, ' ').trim().slice(0, 160);
      return text && unsafeValuePattern.test(text) ? '' : text;
    }
    function valueSummary(value){
      if (Array.isArray(value)) return value.slice(0, 5).map(valueSummary).filter(Boolean).join(', ');
      if (value && typeof value === 'object') return Object.keys(value).filter(keyIsSafe).slice(0, 5).join(', ');
      return textValueSummary(value);
    }
    return Object.keys(details).filter(keyIsSafe).slice(0, 6).map(function(key){
      const value = valueSummary(details[key]);
      return value ? String(key)+': '+value : '';
    }).filter(Boolean).join(' · ');
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
    const warnings = Array.isArray(data && data.warnings) ? data.warnings : [];
    const warningRows = warnings.length ? '<div class="capy-spaces-card"><h3>Import warnings</h3>' +
      '<div class="capy-spaces-muted">Unsupported Space Agent API calls were not imported. Recreate them through safe Capy tools after review.</div>' +
      '<div class="capy-spaces-widget-list">' + warnings.slice(0, 8).map(function(w){
        const api = w && w.api ? String(w.api) : 'unsupported API';
        const message = w && w.message ? String(w.message) : 'Unsupported Space Agent API reference omitted during import.';
        return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(api)+'</strong>' +
          '<div class="capy-spaces-muted">'+escapeHtml(message)+'</div></div></div>';
      }).join('') + '</div></div>' : '';
    return '<div class="capy-spaces-card"><h3>Space Agent import ready</h3>' +
      '<div class="capy-spaces-muted">Imported package metadata only. Generated widget sources remain quarantined/disabled for review by the backend.</div>' +
      '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div><strong>'+escapeHtml(name)+'</strong>' +
      '<div class="capy-spaces-muted">Space ID: '+escapeHtml(spaceId)+' · '+count+' widget'+(count === 1 ? '' : 's')+'</div></div></div>'+widgetRows+'</div></div>' + warningRows;
  }

  function renderSpaceImportError(message){
    return '<div class="capy-spaces-card"><h3>Space Agent import blocked</h3>' +
      '<div class="capy-spaces-muted">'+escapeHtml(message || 'Import payload could not be parsed safely.')+'</div></div>';
  }

  function renderRevisionHistory(spaceId, revisions){
    const safeRevisions = Array.isArray(revisions) ? revisions : [];
    const rows = safeRevisions.length ? safeRevisions.slice(0, 10).map(function(rev){
      const eventId = rev && rev.event_id ? String(rev.event_id) : '';
      const eventType = rev && rev.event_type ? String(rev.event_type) : 'unknown';
      const detailText = formatRevisionDetails(rev && rev.details);
      const previewText = formatRestorePreview(rev && rev.restore_preview);
      const restoreButton = eventId ? '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="restoreRevision" data-space-id="'+escapeHtml(spaceId || '')+'" data-event-id="'+escapeHtml(eventId)+'">Restore</button></div>' : '';
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(eventType)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(formatRevisionTime(rev && rev.created_at))+' · '+escapeHtml(eventId.slice(0, 12) || 'no-event-id')+'</div>' +
        (detailText ? '<div class="capy-spaces-muted">'+escapeHtml(detailText)+'</div>' : '') +
        (previewText ? '<div class="capy-spaces-muted">'+escapeHtml(previewText)+'</div>' : '') +
        '</div>'+restoreButton+'</div>';
    }).join('') : '<div class="capy-spaces-muted">No revision events recorded yet.</div>';
    return '<div class="capy-spaces-card"><h3>Revision history</h3>' +
      '<div class="capy-spaces-muted">Newest safe metadata events. Restore rewrites the Space manifest from a stored snapshot; generated widget bodies are not displayed.</div>' +
      '<div class="capy-spaces-widget-list">'+rows+'</div></div>';
  }

  function formatRevisionDetails(details){
    if (!details || typeof details !== 'object' || Array.isArray(details)) return '';
    const unsafeParts = ['renderer', 'html', 'script', 'data', 'source', 'api_key', 'apikey', 'token', 'password', 'secret', 'credential', 'credentials', 'cookie', 'authorization'];
    const unsafeValuePattern = /(api[_-]?key|apikey|authorization|bearer|cookie|credential|credentials|password|secret|token|<script|<\/script|javascript:|onerror|onload)/i;
    function keyIsSafe(key){
      const lowered = String(key || '').toLowerCase();
      return lowered && !unsafeParts.some(part => lowered.indexOf(part) >= 0);
    }
    function textValueSummary(value){
      const text = String(value == null ? '' : value).replace(/\s+/g, ' ').trim().slice(0, 160);
      return text && unsafeValuePattern.test(text) ? '[REDACTED]' : text;
    }
    function valueSummary(value){
      if (Array.isArray(value)) return value.slice(0, 5).map(valueSummary).filter(Boolean).join(', ');
      if (value && typeof value === 'object') return Object.keys(value).filter(keyIsSafe).slice(0, 5).join(', ');
      return textValueSummary(value);
    }
    function nestedSummaries(value, depth){
      if (!value || typeof value !== 'object' || Array.isArray(value) || depth > 2) return [];
      const rows = [];
      Object.keys(value).filter(keyIsSafe).slice(0, 5).forEach(function(key){
        const child = value[key];
        const summary = valueSummary(child);
        if (summary) rows.push(String(key)+': '+summary);
        if (child && typeof child === 'object' && !Array.isArray(child)) {
          nestedSummaries(child, depth + 1).forEach(function(row){ rows.push(row); });
        }
      });
      return rows;
    }
    const rows = [];
    Object.keys(details).filter(keyIsSafe).slice(0, 6).forEach(function(key){
      const value = valueSummary(details[key]);
      if (value) rows.push(String(key)+': '+value);
      else rows.push(String(key));
      if (details[key] && typeof details[key] === 'object' && !Array.isArray(details[key])) {
        nestedSummaries(details[key], 1).forEach(function(row){ rows.push(row); });
      }
    });
    return rows.filter(Boolean).slice(0, 18).join(' · ');
  }

  function formatRestorePreview(preview){
    if (!preview || typeof preview !== 'object' || Array.isArray(preview)) return '';
    const name = String(preview.name || preview.space_id || 'unnamed snapshot');
    const count = Number(preview.widget_count || 0);
    const countLabel = count === 1 ? '1 widget' : count+' widgets';
    const widgets = Array.isArray(preview.widgets) ? preview.widgets.slice(0, 5).map(function(widget){
      if (!widget || typeof widget !== 'object' || Array.isArray(widget)) return '';
      return [widget.id, widget.title, widget.kind].map(function(part){
        return String(part || '').replace(/\s+/g, ' ').trim().slice(0, 80);
      }).filter(Boolean).join(' / ');
    }).filter(Boolean) : [];
    return 'Preview: '+name+' · '+countLabel+(widgets.length ? ' · Widgets: '+widgets.join(', ') : '');
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

  function renderWidgetEventInbox(events){
    const safeEvents = Array.isArray(events) ? events.slice(0, 10) : [];
    const rows = safeEvents.length ? safeEvents.map(function(event){
      const eventName = event && event.event_name ? String(event.event_name) : 'widget.event';
      const widgetId = event && event.widget_id ? String(event.widget_id) : '';
      const status = event && event.status ? String(event.status) : 'queued';
      const eventId = event && event.event_id ? String(event.event_id) : '';
      const eventTime = event && event.created_at ? formatRevisionTime(event.created_at) : '';
      const eventMeta = eventId || eventTime
        ? '<div class="capy-spaces-muted">'+escapeHtml((eventId ? 'Event: '+eventId : 'Event: unknown') + (eventTime ? ' · '+eventTime : ''))+'</div>'
        : '';
      const details = Object.assign({}, event && event.payload_summary && typeof event.payload_summary === 'object' && !Array.isArray(event.payload_summary) ? event.payload_summary : {});
      if (event && event.prompt_preview) details.prompt = event.prompt_preview;
      const detailText = formatRevisionDetails(details);
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(eventName)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(widgetId)+' · '+escapeHtml(status)+'</div>' +
        eventMeta +
        (detailText ? '<div class="capy-spaces-muted">'+escapeHtml(detailText)+'</div>' : '') +
        '</div></div>';
    }).join('') : '<div class="capy-spaces-muted">No queued widget events.</div>';
    return '<div class="capy-spaces-card"><h3>Queued widget events</h3>' +
      '<div class="capy-spaces-muted">Metadata-only event inbox. Prompts and payload summaries are redacted before display.</div>' +
      '<div class="capy-spaces-widget-list">'+rows+'</div></div>';
  }

  function renderWidgetAgentBridgeStatus(widgetId, events){
    const safeWidgetId = String(widgetId || '').trim();
    if (!safeWidgetId || !Array.isArray(events)) return '';
    const widgetEvents = events.filter(function(event){ return event && String(event.widget_id || '') === safeWidgetId; });
    if (!widgetEvents.length) return '';
    const queuedCount = widgetEvents.filter(function(event){ return String(event.status || 'queued').toLowerCase() === 'queued'; }).length;
    const latest = widgetEvents[0] || {};
    const eventName = safeWeatherText(latest.event_name, 80);
    const status = safeWeatherText(latest.status || 'queued', 40);
    const eventId = safeWeatherText(latest.event_id, 120);
    const parts = ['Agent bridge: '+(queuedCount || widgetEvents.length)+' queued'];
    if (eventName || status) parts.push('Latest: '+[eventName, status].filter(Boolean).join(' · '));
    if (eventId) parts.push('Event: '+eventId);
    return '<div class="capy-spaces-muted capy-spaces-agent-bridge-status">'+escapeHtml(parts.join(' · '))+'</div>';
  }

  function renderWidgetManager(spaceId, widgets, events){
    const widgetCards = widgets.length ? widgets.map(function(w){
      const widgetId = w.id || '';
      const title = w.title || widgetId || 'Untitled widget';
      const kind = w.kind || 'custom';
      const layout = widgetLayout(w);
      return '<div class="capy-spaces-widget" data-widget-id="'+escapeHtml(widgetId)+'">' +
        '<div><strong>'+escapeHtml(title)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(kind)+' · '+escapeHtml(widgetId)+' · '+escapeHtml(formatWidgetLayout(layout))+'</div>' +
        renderWeatherObservation(w.metadata || {}) + renderWidgetAgentBridgeStatus(widgetId, events || []) + '</div>' +
        '<div class="capy-spaces-actions">' +
        '<button type="button" class="capy-spaces-btn" data-capy-action="askWidget" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'" data-widget-title="'+escapeHtml(title)+'">Ask Capy</button>' +
        '<button type="button" class="capy-spaces-btn" data-capy-action="refreshWidget" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'" data-widget-title="'+escapeHtml(title)+'">Refresh</button>' +
        '<button type="button" class="capy-spaces-btn" data-capy-action="viewWidgetDetails" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'">View details</button>' +
        renderMoveButton(spaceId, widgetId, layout, -1, 0, 'Move left') +
        renderMoveButton(spaceId, widgetId, layout, 1, 0, 'Move right') +
        renderMoveButton(spaceId, widgetId, layout, 0, -1, 'Move up') +
        renderMoveButton(spaceId, widgetId, layout, 0, 1, 'Move down') +
        renderResizeButton(spaceId, widgetId, layout, 1, 0, 'Wider') +
        renderResizeButton(spaceId, widgetId, layout, -1, 0, 'Narrower') +
        renderResizeButton(spaceId, widgetId, layout, 0, 1, 'Taller') +
        renderResizeButton(spaceId, widgetId, layout, 0, -1, 'Shorter') +
        renderMinimizeButton(spaceId, widgetId, layout) +
        '<button type="button" class="capy-spaces-btn" data-capy-action="editWidget" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'" data-widget-title="'+escapeHtml(title)+'" data-widget-kind="'+escapeHtml(kind)+'" data-widget-x="'+escapeHtml(layout.x)+'" data-widget-y="'+escapeHtml(layout.y)+'" data-widget-w="'+escapeHtml(layout.w)+'" data-widget-h="'+escapeHtml(layout.h)+'">Edit</button>' +
        '<button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="deleteWidget" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'">Delete</button>' +
        '</div></div>';
    }).join('') : '<div class="capy-spaces-muted">No widgets yet.</div>';
    return '<div class="capy-spaces-card"><button type="button" class="capy-spaces-btn" data-capy-action="reloadSpaces">← Back to spaces</button>' +
      '<h3>Widgets for '+escapeHtml(spaceId)+'</h3>' +
      '<div class="capy-spaces-muted">Safe metadata view. Generated widget code is intentionally not fetched or executed in this list.</div>' +
      '<div class="capy-spaces-widget-list">'+widgetCards+'</div>' +
      renderWidgetEventInbox(events || []) +
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

  function renderWidgetRuntimeContract(contract){
    const safeContract = contract && typeof contract === 'object' && !Array.isArray(contract) ? contract : {};
    const mode = String(safeContract.mode || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const execution = String(safeContract.execution || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    function safeMessageList(values){
      if (!Array.isArray(values)) return '';
      return values.slice(0, 8).map(function(value){
        const text = String(value || '').replace(/\s+/g, ' ').trim().slice(0, 80);
        return /^capy:[a-z0-9:._-]+$/i.test(text) ? text : '';
      }).filter(Boolean).join(', ');
    }
    function safeTokenList(values){
      if (!Array.isArray(values)) return '';
      return values.slice(0, 8).map(function(value){
        const text = String(value || '').replace(/\s+/g, ' ').trim().slice(0, 80);
        return /^[a-z0-9:._-]+$/i.test(text) ? text : '';
      }).filter(Boolean).join(', ');
    }
    const allowed = safeMessageList(safeContract.allowed_messages);
    const blocked = safeMessageList(safeContract.blocked_messages);
    const policy = safeContract.network_policy && typeof safeContract.network_policy === 'object' && !Array.isArray(safeContract.network_policy) ? safeContract.network_policy : {};
    const policyDefault = /^(deny|agent-mediated)$/i.test(String(policy.default || '')) ? String(policy.default || '').toLowerCase() : '';
    const schemes = safeTokenList(policy.allowed_schemes);
    const networkParts = [];
    if (policyDefault) networkParts.push(policyDefault);
    if (schemes) networkParts.push('schemes: '+schemes);
    if (policy.agent_mediated === true) networkParts.push('agent-mediated');
    const network = networkParts.join(' · ');
    const approvals = safeTokenList(safeContract.approval_required_for);
    if (!mode && !execution && !allowed && !blocked && !network && !approvals) return '';
    return '<div class="capy-spaces-widget capy-spaces-runtime-contract"><div><strong>Runtime contract: '+escapeHtml(mode || 'metadata-only')+'</strong>' +
      (execution ? '<div class="capy-spaces-muted">Execution: '+escapeHtml(execution)+'</div>' : '') +
      (allowed ? '<div class="capy-spaces-muted">Allowed messages: '+escapeHtml(allowed)+'</div>' : '') +
      (blocked ? '<div class="capy-spaces-muted">Blocked messages: '+escapeHtml(blocked)+'</div>' : '') +
      (network ? '<div class="capy-spaces-muted">Network policy: '+escapeHtml(network)+'</div>' : '') +
      (approvals ? '<div class="capy-spaces-muted">Approval required: '+escapeHtml(approvals)+'</div>' : '') +
      '</div></div>';
  }

  function safeNotesBody(metadata){
    const notes = metadata && metadata.notes && typeof metadata.notes === 'object' && !Array.isArray(metadata.notes) ? metadata.notes : {};
    if (typeof notes.body === 'string') return notes.body.slice(0, 20000);
    if (Array.isArray(notes.items)) return notes.items.map(function(item){ return String(item || '').slice(0, 500); }).join('\n');
    return '';
  }

  function renderNotesEditor(spaceId, widgetId, kind, metadata){
    const notes = metadata && metadata.notes && typeof metadata.notes === 'object' && !Array.isArray(metadata.notes) ? metadata.notes : {};
    const notesCapable = ['notes', 'rich-text-editor'].indexOf(String(kind || '').toLowerCase()) !== -1 || Object.prototype.hasOwnProperty.call(metadata || {}, 'notes');
    if (!notesCapable) return '';
    const format = typeof notes.format === 'string' && /^[a-z0-9._-]{1,40}$/i.test(notes.format) ? notes.format : 'markdown';
    return '<div class="capy-spaces-card capy-spaces-notes-editor"><h4>Editable notes</h4>' +
      '<div class="capy-spaces-muted">Real note body editing is saved through the typed widget patch API and remains in Space revision history.</div>' +
      '<label>Notes body<textarea id="capyWidgetNotesBody" rows="10" spellcheck="true">'+escapeHtml(safeNotesBody(metadata))+'</textarea></label>' +
      '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn" data-capy-action="saveWidgetNotes" data-space-id="'+escapeHtml(spaceId || '')+'" data-widget-id="'+escapeHtml(widgetId || '')+'" data-notes-format="'+escapeHtml(format)+'">Save notes</button></div>' +
      '</div>';
  }

  function safeWeatherText(value, limit){
    const text = String(value == null ? '' : value).replace(/\s+/g, ' ').trim().slice(0, limit || 160);
    if (!text || /(api[_-]?key|apikey|authorization|bearer|cookie|credential|credentials|password|secret|token|<script|<\/script|javascript:|onerror|onload|renderer|html|script|source)/i.test(text)) return '';
    return text;
  }

  function renderWidgetPrompt(metadata){
    const prompt = metadata && metadata.prompt && typeof metadata.prompt === 'object' && !Array.isArray(metadata.prompt) ? metadata.prompt : {};
    if (!Object.keys(prompt).length) return '';
    const placeholder = safeWeatherText(prompt.placeholder, 180);
    const suggestedEvent = safeWeatherText(prompt.suggested_event, 80);
    if (!placeholder && !suggestedEvent) return '';
    const eventRow = suggestedEvent ? '<div class="capy-spaces-muted">Suggested event: '+escapeHtml(suggestedEvent)+'</div>' : '';
    const promptRow = placeholder ? '<div>'+escapeHtml(placeholder)+'</div>' : '';
    return '<div class="capy-spaces-card capy-weather-card">' +
      '<h4>Suggested prompt</h4>' +
      promptRow +
      eventRow +
      '<div class="capy-spaces-muted">Metadata-only prompt hint. Generated widget bodies stay disabled.</div>' +
      '</div>';
  }

  function renderWeatherObservation(metadata){
    const weather = metadata && metadata.weather && typeof metadata.weather === 'object' && !Array.isArray(metadata.weather) ? metadata.weather : {};
    if (!Object.keys(weather).length) return '';
    const current = weather.current && typeof weather.current === 'object' && !Array.isArray(weather.current) ? weather.current : {};
    const location = [safeWeatherText(weather.location, 80), safeWeatherText(weather.country, 20)].filter(Boolean).join(', ');
    const condition = safeWeatherText(current.condition, 80);
    const temp = safeWeatherText(current.temperature_c, 20);
    const feelsLike = safeWeatherText(current.feels_like_c, 20);
    const status = safeWeatherText(weather.status, 80);
    const summary = safeWeatherText(weather.summary, 240);
    const rows = [];
    if (location) rows.push('<div class="capy-spaces-muted">'+escapeHtml(location)+'</div>');
    if (temp) rows.push('<div><strong>'+escapeHtml(temp)+' °C</strong>'+(feelsLike ? '<span class="capy-spaces-muted"> · Feels like '+escapeHtml(feelsLike)+' °C</span>' : '')+'</div>');
    if (condition) rows.push('<div class="capy-spaces-muted">'+escapeHtml(condition)+'</div>');
    if (status) rows.push('<div class="capy-spaces-muted">Observation status: '+escapeHtml(status)+'</div>');
    if (summary) rows.push('<div class="capy-spaces-muted">'+escapeHtml(summary)+'</div>');
    if (!rows.length) return '';
    return '<div class="capy-spaces-card capy-spaces-weather-observation"><h4>Current weather observation</h4>' +
      '<div class="capy-spaces-muted">Visible metadata-only demo widget state. Network refresh remains agent-mediated.</div>' +
      '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div>'+rows.join('')+'</div></div></div></div>';
  }

  function renderNotesSmokePreview(notesArtifact){
    const artifact = notesArtifact && typeof notesArtifact === 'object' && !Array.isArray(notesArtifact) ? notesArtifact : {};
    const editor = artifact.editor && typeof artifact.editor === 'object' && !Array.isArray(artifact.editor) ? artifact.editor : {};
    const preview = artifact.preview && typeof artifact.preview === 'object' && !Array.isArray(artifact.preview) ? artifact.preview : {};
    const editorMeta = editor.metadata && typeof editor.metadata === 'object' && !Array.isArray(editor.metadata) ? editor.metadata : {};
    const previewMeta = preview.metadata && typeof preview.metadata === 'object' && !Array.isArray(preview.metadata) ? preview.metadata : {};
    const editorNotes = editorMeta.notes && typeof editorMeta.notes === 'object' && !Array.isArray(editorMeta.notes) ? editorMeta.notes : {};
    const previewNotes = previewMeta.notes && typeof previewMeta.notes === 'object' && !Array.isArray(previewMeta.notes) ? previewMeta.notes : {};
    const status = safeWeatherText(editorNotes.status, 80);
    const editorBody = safeWeatherText(editorNotes.body, 360);
    const previewBody = safeWeatherText(previewNotes.body, 360);
    const format = safeWeatherText(previewNotes.format || editorNotes.format, 40);
    const rows = [];
    if (status) rows.push('<div class="capy-spaces-muted">Draft status: '+escapeHtml(status)+'</div>');
    if (format) rows.push('<div class="capy-spaces-muted">Format: '+escapeHtml(format)+'</div>');
    if (editorBody) rows.push('<div>'+escapeHtml(editorBody)+'</div>');
    if (previewBody) rows.push('<div class="capy-spaces-muted">Preview: '+escapeHtml(previewBody)+'</div>');
    if (!rows.length) return '';
    return '<div class="capy-spaces-card capy-spaces-notes-smoke"><h4>Saved notes preview</h4>' +
      '<div class="capy-spaces-muted">Visible metadata-only notes demo state. Rich editing and attachments remain agent-mediated.</div>' +
      '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div>'+rows.join('')+'</div></div></div></div>';
  }

  function renderKanbanSmokePreview(kanbanBoard){
    const board = kanbanBoard && typeof kanbanBoard === 'object' && !Array.isArray(kanbanBoard) ? kanbanBoard : {};
    const columns = Array.isArray(board.columns) ? board.columns : [];
    const status = safeWeatherText(board.status, 80);
    const columnCount = Number(board.column_count || columns.length || 0);
    const columnRows = columns.slice(0, 8).map(function(column){
      const meta = column && column.metadata && typeof column.metadata === 'object' && !Array.isArray(column.metadata) ? column.metadata : {};
      const kanban = meta.kanban && typeof meta.kanban === 'object' && !Array.isArray(meta.kanban) ? meta.kanban : {};
      const label = safeWeatherText(kanban.column || (column && column.title), 80);
      const cards = Array.isArray(kanban.cards) ? kanban.cards : [];
      const cardRows = cards.slice(0, 6).map(function(card){
        const title = safeWeatherText(card && card.title, 120);
        const cardStatus = safeWeatherText(card && card.status, 40);
        if (!title) return '';
        return '<div class="capy-spaces-muted">• '+escapeHtml(title)+(cardStatus ? ' · '+escapeHtml(cardStatus) : '')+'</div>';
      }).filter(Boolean).join('');
      if (!label && !cardRows) return '';
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(label || 'Column')+'</strong>'+cardRows+'</div></div>';
    }).filter(Boolean).join('');
    if (!status && !columnRows) return '';
    return '<div class="capy-spaces-card capy-spaces-kanban-smoke"><h4>Kanban board preview</h4>' +
      '<div class="capy-spaces-muted">Visible metadata-only board state. Drag/drop and card edits remain typed API operations.</div>' +
      '<div class="capy-spaces-muted">Status: '+escapeHtml(status || 'board-ready')+' · Columns: '+columnCount+'</div>' +
      '<div class="capy-spaces-widget-list">'+columnRows+'</div></div>';
  }

  function renderWidgetDetailPanel(spaceId, widget, runtimeContract){
    const safeWidget = widget && typeof widget === 'object' ? widget : {};
    const widgetId = safeWidget.id || '';
    const title = safeWidget.title || widgetId || 'Untitled widget';
    const kind = safeWidget.kind || 'custom';
    const layout = widgetLayout(safeWidget);
    const recovery = safeWidget.recovery && typeof safeWidget.recovery === 'object' ? safeWidget.recovery : {};
    const recoveryText = recovery.disabled ? 'Recovery: disabled' : 'Recovery: enabled';
    const revision = safeWidget.revision_event_id ? ' · Revision: '+escapeHtml(safeWidget.revision_event_id) : '';
    const metadata = safeWidget.metadata && typeof safeWidget.metadata === 'object' && !Array.isArray(safeWidget.metadata) ? safeWidget.metadata : {};
    const metadataText = formatRevisionDetails(metadata || {});
    const metadataRow = metadataText ? '<div class="capy-spaces-muted">Metadata: '+escapeHtml(metadataText)+'</div>' : '';
    const eventBridge = safeWidget.event_bridge && typeof safeWidget.event_bridge === 'object' && !Array.isArray(safeWidget.event_bridge) ? safeWidget.event_bridge : {};
    const eventBridgeText = formatRevisionDetails({event_bridge: eventBridge});
    const eventBridgeRow = eventBridgeText ? '<div class="capy-spaces-muted">'+escapeHtml(eventBridgeText)+'</div>' : '';
    const notesEditor = renderNotesEditor(spaceId, widgetId, kind, metadata);
    const exportMeta = metadata.export && typeof metadata.export === 'object' && !Array.isArray(metadata.export) ? metadata.export : {};
    const pdfExportAction = exportMeta.pdf ? '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn" data-capy-action="requestWidgetPdfExport" data-space-id="'+escapeHtml(spaceId || '')+'" data-widget-id="'+escapeHtml(widgetId)+'" data-widget-title="'+escapeHtml(title)+'">Request PDF export</button></div>' : '';
    return '<div class="capy-spaces-card" data-widget-detail-id="'+escapeHtml(widgetId)+'">' +
      '<button type="button" class="capy-spaces-btn" data-capy-action="loadWidgets" data-space-id="'+escapeHtml(spaceId || '')+'">← Back to widgets</button>' +
      '<h3>Widget details</h3>' +
      '<div class="capy-spaces-muted">Metadata-only detail. Generated bodies are not displayed or executed.</div>' +
      '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div><strong>'+escapeHtml(title)+'</strong>' +
      '<div class="capy-spaces-muted">'+escapeHtml(kind)+' · '+escapeHtml(widgetId)+' · '+escapeHtml(formatWidgetLayout(layout))+'</div>' +
      '<div class="capy-spaces-muted">Space ID: '+escapeHtml(spaceId || '')+' · '+escapeHtml(recoveryText)+revision+'</div>' +
      metadataRow +
      eventBridgeRow +
      renderWidgetRuntimeContract(runtimeContract) +
      '</div>'+pdfExportAction+'</div></div>' + renderWidgetPrompt(metadata) + renderWeatherObservation(metadata) + notesEditor + '</div>';
  }


  function layoutNumber(value, fallback, min, max){
    const parsed = parseInt(value, 10);
    const n = Number.isFinite(parsed) ? parsed : fallback;
    return Math.max(min, Math.min(max, n));
  }

  function layoutBoolean(value){
    if (value === true) return true;
    return ['1', 'true', 'yes', 'on'].indexOf(String(value || '').trim().toLowerCase()) !== -1;
  }

  function renderMoveButton(spaceId, widgetId, layout, dx, dy, label){
    return '<button type="button" class="capy-spaces-btn" data-capy-action="moveWidget" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'" data-widget-x="'+escapeHtml(layout.x)+'" data-widget-y="'+escapeHtml(layout.y)+'" data-widget-w="'+escapeHtml(layout.w)+'" data-widget-h="'+escapeHtml(layout.h)+'" data-move-dx="'+escapeHtml(dx)+'" data-move-dy="'+escapeHtml(dy)+'">'+escapeHtml(label)+'</button>';
  }

  function renderResizeButton(spaceId, widgetId, layout, dw, dh, label){
    return '<button type="button" class="capy-spaces-btn" data-capy-action="resizeWidget" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'" data-widget-x="'+escapeHtml(layout.x)+'" data-widget-y="'+escapeHtml(layout.y)+'" data-widget-w="'+escapeHtml(layout.w)+'" data-widget-h="'+escapeHtml(layout.h)+'" data-resize-dw="'+escapeHtml(dw)+'" data-resize-dh="'+escapeHtml(dh)+'">'+escapeHtml(label)+'</button>';
  }

  function renderMinimizeButton(spaceId, widgetId, layout){
    const label = layout.minimized ? 'Restore' : 'Minimize';
    return '<button type="button" class="capy-spaces-btn" data-capy-action="toggleWidgetMinimized" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'" data-widget-x="'+escapeHtml(layout.x)+'" data-widget-y="'+escapeHtml(layout.y)+'" data-widget-w="'+escapeHtml(layout.w)+'" data-widget-h="'+escapeHtml(layout.h)+'" data-widget-minimized="'+escapeHtml(layout.minimized ? 'true' : 'false')+'">'+escapeHtml(label)+'</button>';
  }

  function widgetLayout(widget){
    const raw = widget && widget.layout && typeof widget.layout === 'object' ? widget.layout : {};
    return {
      x: layoutNumber(raw.x, 0, 0, 10000),
      y: layoutNumber(raw.y, 0, 0, 10000),
      w: layoutNumber(raw.w, 6, 1, 24),
      h: layoutNumber(raw.h, 4, 1, 24),
      minimized: layoutBoolean(raw.minimized),
    };
  }

  function formLayout(root){
    return {
      x: layoutNumber(getInputValue(root, '#capyWidgetX'), 0, 0, 10000),
      y: layoutNumber(getInputValue(root, '#capyWidgetY'), 0, 0, 10000),
      w: layoutNumber(getInputValue(root, '#capyWidgetW'), 6, 1, 24),
      h: layoutNumber(getInputValue(root, '#capyWidgetH'), 4, 1, 24),
    };
  }

  function moveWidgetBy(button){
    const layout = {
      x: layoutNumber(button.dataset.widgetX, 0, 0, 10000),
      y: layoutNumber(button.dataset.widgetY, 0, 0, 10000),
      w: layoutNumber(button.dataset.widgetW, 6, 1, 24),
      h: layoutNumber(button.dataset.widgetH, 4, 1, 24),
    };
    const dx = layoutNumber(button.dataset.moveDx, 0, -24, 24);
    const dy = layoutNumber(button.dataset.moveDy, 0, -24, 24);
    return {
      x: layoutNumber(layout.x + dx, 0, 0, 10000),
      y: layoutNumber(layout.y + dy, 0, 0, 10000),
      w: layout.w,
      h: layout.h,
    };
  }

  function resizeWidgetBy(button){
    const layout = {
      x: layoutNumber(button.dataset.widgetX, 0, 0, 10000),
      y: layoutNumber(button.dataset.widgetY, 0, 0, 10000),
      w: layoutNumber(button.dataset.widgetW, 6, 1, 24),
      h: layoutNumber(button.dataset.widgetH, 4, 1, 24),
    };
    const dw = layoutNumber(button.dataset.resizeDw, 0, -24, 24);
    const dh = layoutNumber(button.dataset.resizeDh, 0, -24, 24);
    return {
      x: layout.x,
      y: layout.y,
      w: layoutNumber(layout.w + dw, 6, 1, 24),
      h: layoutNumber(layout.h + dh, 4, 1, 24),
    };
  }

  function toggleWidgetMinimized(button){
    return {
      x: layoutNumber(button.dataset.widgetX, 0, 0, 10000),
      y: layoutNumber(button.dataset.widgetY, 0, 0, 10000),
      w: layoutNumber(button.dataset.widgetW, 6, 1, 24),
      h: layoutNumber(button.dataset.widgetH, 4, 1, 24),
      minimized: !layoutBoolean(button.dataset.widgetMinimized),
    };
  }

  function getInputValue(root, selector){
    const input = getRootInput(root, selector);
    return input ? input.value : '';
  }

  function formatWidgetLayout(layout){
    const state = layout.minimized ? ' · minimized' : '';
    return 'x'+layout.x+' y'+layout.y+' · '+layout.w+'×'+layout.h+state;
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
      if (typeof syncCapyActiveSpaceContext === 'function') syncCapyActiveSpaceContext();
      await loadCapySpaces();
      return;
    }
    if (action === 'clearActiveSpace') {
      const sessionId = currentSessionId();
      if (!sessionId) return;
      const data = await postSpacesJson('api/spaces/deactivate', {session_id: sessionId});
      if (data && data.session && typeof S !== 'undefined') S.session = data.session;
      else if (typeof S !== 'undefined' && S.session) S.session.active_space_id = null;
      if (typeof syncCapyActiveSpaceContext === 'function') syncCapyActiveSpaceContext();
      await loadCapySpaces();
      return;
    }
    if (action === 'createSpaceFromSession') {
      const sessionId = currentSessionId();
      if (!sessionId) return;
      const data = await postSpacesJson('api/spaces/create-from-session', {session_id: sessionId});
      if (data && data.session && typeof S !== 'undefined') S.session = data.session;
      if (typeof syncCapyActiveSpaceContext === 'function') syncCapyActiveSpaceContext();
      await loadCapySpaces();
      return;
    }
    if (action === 'addSystemWidget') {
      const panel = button.dataset.systemPanel || '';
      if (['chat', 'workspaces', 'tasks', 'memory', 'settings'].indexOf(panel) === -1) return;
      if (!spaceId) return;
      await postSpacesJson('api/spaces/system-widget/upsert', {space_id: spaceId, panel: panel, layout: {x: 0, y: 0, w: 12, h: 6}});
      await loadSpaceWidgets(spaceId);
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
    if (action === 'importSpaceAgentZip') {
      const root = document.getElementById('capySpacesRoot');
      const archiveInput = getRootInput(root, '#capySpaceAgentImportZipB64');
      const archiveB64 = archiveInput && archiveInput.value ? String(archiveInput.value).trim() : '';
      const data = await postSpacesJson('api/spaces/import', {archive_b64: archiveB64});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) refreshedRoot.innerHTML = renderSpaceImportResult(data || {}) + refreshedRoot.innerHTML;
      return;
    }
    if (action === 'runDemoSmoke') {
      const demo = button.dataset.demo || '';
      if (!demo) return;
      const data = await postSpacesJson('api/spaces/demo/run', {demo: demo});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) refreshedRoot.innerHTML = renderDemoSmokeResult(data || {}) + refreshedRoot.innerHTML;
      return;
    }
    if (action === 'runAllDemoSmokes') {
      const data = await postSpacesJson('api/spaces/demo/run-all', {});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) refreshedRoot.innerHTML = renderDemoSmokeSuiteResult(data || {}) + refreshedRoot.innerHTML;
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
      const result = await postSpacesJson('api/spaces/templates/install', {template: 'weather'});
      await loadCapySpaces();
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderTemplateInstallStatus(result || {}) + root.innerHTML;
      return;
    }
    if (action === 'installResearchTemplate') {
      const result = await postSpacesJson('api/spaces/templates/install', {template: 'research'});
      await loadCapySpaces();
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderTemplateInstallStatus(result || {}) + root.innerHTML;
      return;
    }
    if (action === 'installDashboardTemplate') {
      const result = await postSpacesJson('api/spaces/templates/install', {template: 'dashboard'});
      await loadCapySpaces();
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderTemplateInstallStatus(result || {}) + root.innerHTML;
      return;
    }
    if (action === 'installCameraTemplate') {
      const result = await postSpacesJson('api/spaces/templates/install', {template: 'camera'});
      await loadCapySpaces();
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderTemplateInstallStatus(result || {}) + root.innerHTML;
      return;
    }
    if (action === 'installKanbanTemplate') {
      const result = await postSpacesJson('api/spaces/templates/install', {template: 'kanban'});
      await loadCapySpaces();
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderTemplateInstallStatus(result || {}) + root.innerHTML;
      return;
    }
    if (action === 'installNotesTemplate') {
      const result = await postSpacesJson('api/spaces/templates/install', {template: 'notes'});
      await loadCapySpaces();
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderTemplateInstallStatus(result || {}) + root.innerHTML;
      return;
    }
    if (action === 'installBrowserTemplate') {
      const result = await postSpacesJson('api/spaces/templates/install', {template: 'browser'});
      await loadCapySpaces();
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderTemplateInstallStatus(result || {}) + root.innerHTML;
      return;
    }
    if (action === 'installStockTemplate') {
      const result = await postSpacesJson('api/spaces/templates/install', {template: 'stock'});
      await loadCapySpaces();
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderTemplateInstallStatus(result || {}) + root.innerHTML;
      return;
    }
    if (action === 'installServiceTemplate') {
      const result = await postSpacesJson('api/spaces/templates/install', {template: 'service'});
      await loadCapySpaces();
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderTemplateInstallStatus(result || {}) + root.innerHTML;
      return;
    }
    if (action === 'installModelSetupTemplate') {
      await postSpacesJson('api/spaces/templates/install', {template: 'model-setup'});
      await loadCapySpaces();
      return;
    }
    if (action === 'installGameTemplate') {
      await postSpacesJson('api/spaces/templates/install', {template: 'game'});
      await loadCapySpaces();
      return;
    }
    if (action === 'installMusicTemplate') {
      await postSpacesJson('api/spaces/templates/install', {template: 'music'});
      await loadCapySpaces();
      return;
    }
    if (action === 'installBigBangTemplate') {
      await postSpacesJson('api/spaces/templates/install', {template: 'big-bang'});
      await loadCapySpaces();
      return;
    }
    if (action === 'resetBigBangTemplate') {
      if (!spaceId || typeof showConfirmDialog !== 'function') return;
      const ok = await showConfirmDialog({title: 'Reset Big Bang onboarding?', message: 'Reset this onboarding Space to the canonical safe demo metadata? Extra generated widgets will be removed from the active manifest, with revision history preserved.', confirmLabel: 'Reset onboarding', danger: true, focusCancel: true});
      if (!ok) return;
      const result = await postSpacesJson('api/spaces/templates/reset', {template: 'big-bang', space_id: spaceId});
      await loadCapySpaces();
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderTemplateResetStatus(result) + root.innerHTML;
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
    if (action === 'restoreRevision') {
      const eventId = button.dataset.eventId || '';
      if (!spaceId || !eventId || typeof showConfirmDialog !== 'function') return;
      const ok = await showConfirmDialog({title: 'Restore Space revision?', message: 'Restore space "'+spaceId+'" to revision '+eventId.slice(0, 12)+'? The current manifest remains in revision history.', confirmLabel: 'Restore revision', danger: true, focusCancel: true});
      if (!ok) return;
      await postSpacesJson('api/spaces/revision/restore', {space_id: spaceId, event_id: eventId});
      await openSpaceDetail(spaceId);
      return;
    }
    if (action === 'deleteSharedData') {
      const dataKey = safeSharedDataKey(button.dataset.dataKey || '');
      if (!spaceId || !dataKey || typeof showConfirmDialog !== 'function') return;
      const ok = await showConfirmDialog({title: 'Delete shared data slot?', message: 'Delete shared data slot "'+dataKey+'" from space "'+spaceId+'"? Raw data is not displayed in this UI.', confirmLabel: 'Delete data slot', danger: true, focusCancel: true});
      if (!ok) return;
      await postSpacesJson('api/spaces/data/delete', {space_id: spaceId, key: dataKey});
      await openSpaceDetail(spaceId);
      return;
    }
    if (action === 'viewWidgetDetails') {
      const widgetId = button.dataset.widgetId || '';
      const root = document.getElementById('capySpacesRoot');
      if (!spaceId || !widgetId || !root) return;
      const data = await fetchSpacesJson('api/spaces/widget?space_id='+encodeURIComponent(spaceId)+'&widget_id='+encodeURIComponent(widgetId));
      let runtimeContract = null;
      try {
        const contractData = await postSpacesJson('api/spaces/tool', {
          action: 'space.widget.runtime_contract',
          space_id: spaceId,
          widget_id: widgetId,
        });
        runtimeContract = contractData && contractData.contract;
      } catch (contractErr) {
        runtimeContract = null;
      }
      root.innerHTML = renderWidgetDetailPanel(spaceId, data && data.widget, runtimeContract) + root.innerHTML;
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
    if (action === 'moveWidget') {
      const widgetId = button.dataset.widgetId || '';
      if (!spaceId || !widgetId) return;
      await postSpacesJson('api/spaces/widget/patch', {
        space_id: spaceId,
        widget_id: widgetId,
        patch: {layout: moveWidgetBy(button)},
      });
      await loadSpaceWidgets(spaceId);
      return;
    }
    if (action === 'resizeWidget') {
      const widgetId = button.dataset.widgetId || '';
      if (!spaceId || !widgetId) return;
      await postSpacesJson('api/spaces/widget/patch', {
        space_id: spaceId,
        widget_id: widgetId,
        patch: {layout: resizeWidgetBy(button)},
      });
      await loadSpaceWidgets(spaceId);
      return;
    }
    if (action === 'toggleWidgetMinimized') {
      const widgetId = button.dataset.widgetId || '';
      if (!spaceId || !widgetId) return;
      await postSpacesJson('api/spaces/widget/patch', {
        space_id: spaceId,
        widget_id: widgetId,
        patch: {layout: toggleWidgetMinimized(button)},
      });
      await loadSpaceWidgets(spaceId);
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
      const result = await postSpacesJson('api/spaces/widget/event', {
        space_id: spaceId,
        widget_id: widgetId,
        event_name: 'agent.prompt',
        prompt: promptText,
        payload: {source: 'widget-manager', widget_title: widgetTitle},
      });
      await loadSpaceWidgets(spaceId);
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderWidgetEventQueuedStatus(result || {}) + root.innerHTML;
      return;
    }
    if (action === 'refreshWidget') {
      const widgetId = button.dataset.widgetId || '';
      if (!spaceId || !widgetId) return;
      const result = await postSpacesJson('api/spaces/widget/event', {
        space_id: spaceId,
        widget_id: widgetId,
        event_name: 'widget.refresh',
        payload: {source: 'widget-manager', action: 'refresh'},
      });
      await loadSpaceWidgets(spaceId);
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderWidgetEventQueuedStatus(result || {}) + root.innerHTML;
      return;
    }
    if (action === 'requestWidgetPdfExport') {
      const widgetId = button.dataset.widgetId || '';
      const widgetTitle = button.dataset.widgetTitle || widgetId;
      if (!spaceId || !widgetId) return;
      await postSpacesJson('api/spaces/widget/event', {
        space_id: spaceId,
        widget_id: widgetId,
        event_name: 'widget.export.pdf',
        payload: {source: 'widget-detail', action: 'export_pdf', widget_title: widgetTitle},
      });
      await loadSpaceWidgets(spaceId);
      return;
    }
    if (action === 'saveWidgetNotes') {
      const widgetId = button.dataset.widgetId || '';
      const root = document.getElementById('capySpacesRoot');
      const notesInput = getRootInput(root, '#capyWidgetNotesBody');
      const format = /^[a-z0-9._-]{1,40}$/i.test(String(button.dataset.notesFormat || '')) ? String(button.dataset.notesFormat || '') : 'markdown';
      if (!spaceId || !widgetId || !notesInput || !root) return;
      await postSpacesJson('api/spaces/widget/patch', {
        space_id: spaceId,
        widget_id: widgetId,
        patch: {notes: {body: String(notesInput.value || ''), format: format, updated_from: 'spaces-ui'}},
      });
      const data = await fetchSpacesJson('api/spaces/widget?space_id='+encodeURIComponent(spaceId)+'&widget_id='+encodeURIComponent(widgetId));
      root.innerHTML = renderWidgetDetailPanel(spaceId, data && data.widget, null) + root.innerHTML;
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
      const widgetId = button.dataset.widgetId || '';
      if (!spaceId || !widgetId || typeof showConfirmDialog !== 'function') return;
      const ok = await showConfirmDialog({title: 'Delete widget?', message: 'Delete widget "'+widgetId+'"? This removes it from the active Space manifest, with revision history preserved.', confirmLabel: 'Delete widget', danger: true, focusCancel: true});
      if (!ok) return;
      await postSpacesJson('api/spaces/widget/delete', {space_id: spaceId, widget_id: widgetId});
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

  function renderRecoveryRevisionRows(spaceId, revisions){
    const safeRevisions = Array.isArray(revisions) ? revisions.slice(0, 5) : [];
    if (!safeRevisions.length) return '<div class="capy-spaces-muted">No recovery rollback points yet.</div>';
    return safeRevisions.map(function(rev){
      const eventId = rev && rev.event_id ? String(rev.event_id) : '';
      const eventType = rev && rev.event_type ? String(rev.event_type) : 'revision';
      const detailText = formatRevisionDetails(rev && rev.details);
      const restoreButton = eventId ? '<button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="restoreRecoveryRevision" data-space-id="'+escapeHtml(spaceId)+'" data-event-id="'+escapeHtml(eventId)+'">Restore revision</button>' : '';
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(eventType)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(formatRevisionTime(rev && rev.created_at))+' · '+escapeHtml(eventId.slice(0, 12) || 'no-event-id')+'</div>' +
        (detailText ? '<div class="capy-spaces-muted">'+escapeHtml(detailText)+'</div>' : '') +
        '</div><div class="capy-spaces-actions">'+restoreButton+'</div></div>';
    }).join('');
  }

  function renderRecoveryWidgetEventStatus(widget){
    const count = Number(widget && widget.queued_event_count || 0);
    if (!count) return '';
    const latest = widget && widget.latest_queued_event && typeof widget.latest_queued_event === 'object' ? widget.latest_queued_event : {};
    const parts = ['Queued events: '+count];
    const eventName = latest.event_name ? String(latest.event_name) : '';
    const status = latest.status ? String(latest.status) : '';
    if (eventName || status) parts.push([eventName, status].filter(Boolean).join(' · '));
    if (latest.event_id) parts.push('Event: '+String(latest.event_id).slice(0, 12));
    return '<div class="capy-spaces-muted">'+escapeHtml(parts.join(' · '))+'</div>';
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
      const spaceDisabled = !!s.disabled;
      const spaceDisabledReason = s.disabled_reason ? String(s.disabled_reason) : '';
      const spaceStatus = spaceDisabled ? '<div class="capy-spaces-muted">Space disabled'+(spaceDisabledReason ? ': '+escapeHtml(spaceDisabledReason) : '')+'</div>' : '';
      const spaceAction = spaceDisabled
        ? '<button type="button" class="capy-spaces-btn" data-capy-action="enableRecoverySpace" data-space-id="'+escapeHtml(spaceId)+'">Enable space</button>'
        : '<button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="disableRecoverySpace" data-space-id="'+escapeHtml(spaceId)+'">Disable space</button>';
      const widgetRows = widgets.length ? '<div class="capy-spaces-widget-list">'+widgets.map(function(w){
        const widgetId = w && w.id ? String(w.id) : '';
        const title = w && w.title ? String(w.title) : widgetId || 'Untitled widget';
        const kind = w && w.kind ? String(w.kind) : 'custom';
        const disabled = !!(w && w.disabled);
        const disabledReason = w && w.disabled_reason ? String(w.disabled_reason) : '';
        return '<div class="capy-spaces-widget" data-widget-id="'+escapeHtml(widgetId)+'"><div><strong>'+escapeHtml(title)+'</strong>' +
          '<div class="capy-spaces-muted">'+escapeHtml(kind)+' · '+escapeHtml(widgetId)+(disabled ? ' · Disabled'+(disabledReason ? ': '+escapeHtml(disabledReason) : '') : '')+'</div>' +
          renderRecoveryWidgetEventStatus(w || {}) +
          '</div>' +
          '<div class="capy-spaces-actions">' +
          '<button type="button" class="capy-spaces-btn" data-capy-action="repairRecoveryWidget" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'" data-widget-title="'+escapeHtml(title)+'">Ask Capy to repair</button>' +
          (disabled ? '<button type="button" class="capy-spaces-btn" data-capy-action="enableRecoveryWidget" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'">Enable widget</button>' : '<button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="disableRecoveryWidget" data-space-id="'+escapeHtml(spaceId)+'" data-widget-id="'+escapeHtml(widgetId)+'">Disable widget</button>') +
          '</div></div>';
      }).join('')+'</div>' : '<div class="capy-spaces-muted">No widget metadata available for this space.</div>';
      const recoveryRows = renderRecoveryRevisionRows(spaceId, s.revisions || []);
      return '<div class="capy-spaces-card"><h3>'+escapeHtml(name)+'</h3>' +
        (description ? '<div class="capy-spaces-muted">'+escapeHtml(description)+'</div>' : '') +
        spaceStatus +
        '<div class="capy-spaces-muted">Space ID: '+escapeHtml(spaceId)+' · Widgets: '+Number(s.widget_count||0)+' · Revision: '+escapeHtml(s.revision_event_id||'none')+'</div>' +
        '<div class="capy-spaces-actions">'+spaceAction+'</div>' +
        widgetRows +
        '<div class="capy-spaces-card"><h4>Recovery rollback</h4><div class="capy-spaces-muted">Restore safe metadata snapshots without rendering generated widget bodies.</div><div class="capy-spaces-widget-list">'+recoveryRows+'</div></div>' +
        '</div></div>';
    }).join('') : '<div class="capy-spaces-muted">No spaces found in recovery metadata.</div>';
    return '<div class="capy-spaces-card"><h3>Safe recovery</h3>' +
      '<div class="capy-spaces-muted">Generated widgets rendered: '+String(!!data.generated_widgets_rendered)+'. This panel lists metadata only so broken generated UI cannot execute here.</div>' +
      '<div class="capy-spaces-widget-list">'+rows+'</div></div>';
  }

  async function handleCapySpacesRecoveryClick(event){
    const button = event.target && event.target.closest ? event.target.closest('[data-capy-action]') : null;
    if (!button) return;
    const action = button.dataset.capyAction;
    if (action !== 'disableRecoveryWidget' && action !== 'enableRecoveryWidget' && action !== 'disableRecoverySpace' && action !== 'enableRecoverySpace' && action !== 'repairRecoveryWidget' && action !== 'restoreRecoveryRevision') return;
    const spaceId = button.dataset.spaceId || '';
    if (!spaceId) return;
    if (action === 'repairRecoveryWidget') {
      if (typeof showPromptDialog !== 'function') return;
      const widgetId = button.dataset.widgetId || '';
      const widgetTitle = button.dataset.widgetTitle || widgetId;
      if (!widgetId) return;
      const promptText = await showPromptDialog({
        title: 'Ask Capy to repair widget',
        placeholder: 'Describe what is broken in '+widgetTitle,
        confirmLabel: 'Queue repair',
      });
      if (!promptText) return;
      await postSpacesJson('api/spaces/widget/event', {
        space_id: spaceId,
        widget_id: widgetId,
        event_name: 'agent.repair',
        prompt: promptText,
        payload: {source: 'recovery-panel', action: 'repair', widget_title: widgetTitle},
      });
      await loadCapySpacesRecovery();
      return;
    }
    if (typeof showConfirmDialog !== 'function') return;
    if (action === 'restoreRecoveryRevision') {
      const eventId = button.dataset.eventId || '';
      if (!eventId) return;
      const ok = await showConfirmDialog({title: 'Restore recovery revision?', message: 'Restore Space "'+spaceId+'" to revision '+eventId.slice(0, 12)+' from safe recovery? The current manifest remains in revision history, and generated widget bodies are not displayed here.', confirmLabel: 'Restore revision', danger: true, focusCancel: true});
      if (!ok) return;
      await postSpacesJson('api/spaces/revision/restore', {space_id: spaceId, event_id: eventId});
      await loadCapySpacesRecovery();
      return;
    }
    if (action === 'disableRecoverySpace') {
      const ok = await showConfirmDialog({title: 'Disable space?', message: 'Disable Space "'+spaceId+'" from safe recovery? The manifest and widgets are preserved for repair/rollback.', confirmLabel: 'Disable space', danger: true, focusCancel: true});
      if (!ok) return;
      await postSpacesJson('api/spaces/recovery/disable-space', {space_id: spaceId, reason: 'disabled from recovery panel'});
      await loadCapySpacesRecovery();
      return;
    }
    if (action === 'enableRecoverySpace') {
      const ok = await showConfirmDialog({title: 'Enable space?', message: 'Re-enable Space "'+spaceId+'" from safe recovery? Generated widgets are still not rendered in recovery.', confirmLabel: 'Enable space', danger: true, focusCancel: true});
      if (!ok) return;
      await postSpacesJson('api/spaces/recovery/enable-space', {space_id: spaceId, reason: 'enabled from recovery panel'});
      await loadCapySpacesRecovery();
      return;
    }
    const widgetId = button.dataset.widgetId || '';
    if (!widgetId) return;
    if (action === 'disableRecoveryWidget') {
      const ok = await showConfirmDialog({title: 'Disable widget?', message: 'Disable widget "'+widgetId+'" from safe recovery? The source is preserved for repair/rollback.', confirmLabel: 'Disable widget', danger: true, focusCancel: true});
      if (!ok) return;
      await postSpacesJson('api/spaces/recovery/disable-widget', {space_id: spaceId, widget_id: widgetId, reason: 'disabled from recovery panel'});
      await loadCapySpacesRecovery();
      return;
    }
    const ok = await showConfirmDialog({title: 'Enable widget?', message: 'Re-enable widget "'+widgetId+'" from safe recovery? Generated content is still not rendered in recovery.', confirmLabel: 'Enable widget', danger: true, focusCancel: true});
    if (!ok) return;
    await postSpacesJson('api/spaces/recovery/enable-widget', {space_id: spaceId, widget_id: widgetId, reason: 'enabled from recovery panel'});
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
