// Capy Spaces foundation shell.
// This UI exposes safe metadata and widget management without executing widget renderers.
(function(){
  var handlersBound = false;
  var recoveryHandlersBound = false;
  var runtimeMessagesBound = false;
  var widgetRuntimeSessions = {};

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
    const title = template === 'weather' ? 'Weather demo installed' : (template === 'notes' ? 'Notes app installed' : (template === 'kanban' ? 'Kanban board installed' : (template === 'research' ? 'Research harness installed' : (template === 'dashboard' ? 'Dashboard demo installed' : (template === 'camera' ? 'Camera dashboard installed' : (template === 'browser' ? 'Browser surface installed' : (template === 'stock' ? 'Stock chart installed' : (template === 'service' ? 'Local service dashboard installed' : (template === 'model-setup' ? 'Model setup installed' : (template === 'music' ? 'Music sequencer installed' : (template === 'game' ? 'Game sandbox installed' : 'Template installed')))))))))));
    const openLabel = template === 'weather' ? 'Open weather demo' : (template === 'notes' ? 'Open notes app' : (template === 'kanban' ? 'Open kanban board' : (template === 'research' ? 'Open research harness' : (template === 'dashboard' ? 'Open dashboard demo' : (template === 'camera' ? 'Open camera dashboard' : (template === 'browser' ? 'Open browser surface' : (template === 'stock' ? 'Open stock chart' : (template === 'service' ? 'Open local service dashboard' : (template === 'model-setup' ? 'Open model setup' : (template === 'music' ? 'Open music sequencer' : (template === 'game' ? 'Open game sandbox' : 'Open Space')))))))))));
    const manageLabel = template === 'weather' ? 'Manage weather widget' : (template === 'notes' ? 'Manage notes widgets' : (template === 'kanban' ? 'Manage kanban widgets' : (template === 'research' ? 'Manage research widgets' : (template === 'dashboard' ? 'Manage dashboard widgets' : (template === 'camera' ? 'Manage camera widgets' : (template === 'browser' ? 'Manage browser widgets' : (template === 'stock' ? 'Manage stock widgets' : (template === 'service' ? 'Manage service widgets' : (template === 'model-setup' ? 'Manage provider widgets' : (template === 'music' ? 'Manage music widgets' : (template === 'game' ? 'Manage game widgets' : 'Manage widgets')))))))))));
    const widgetItems = widgets.slice(0, 6).map(function(w){
      return '<li>'+escapeHtml(w.title || w.id || 'Widget')+'</li>';
    }).join('');
    const smokeDemos = {
      weather: ['demo_weather_widget', 'Run weather smoke'],
      notes: ['demo_notes_app', 'Run notes smoke'],
      kanban: ['demo_kanban_board', 'Run kanban smoke'],
      research: ['demo_research_harness_pdf_export', 'Run research smoke'],
      dashboard: ['demo_daily_dashboard', 'Run dashboard smoke'],
      camera: ['demo_camera_dashboard', 'Run camera smoke'],
      browser: ['demo_browser_cocontrol_google_or_test_site', 'Run browser smoke'],
      stock: ['demo_stock_chart', 'Run stock smoke'],
      service: ['demo_local_agent_control_dashboard', 'Run local service smoke'],
      'model-setup': ['demo_provider_setup', 'Run provider setup smoke'],
      music: ['demo_step_sequencer_piano_roll', 'Run music smoke'],
      game: ['demo_snake_iterative_repair', 'Run snake smoke']
    };
    const smokeMeta = smokeDemos[template];
    const smokeAction = smokeMeta
      ? '<button type="button" class="capy-spaces-btn" data-capy-action="runDemoSmoke" data-demo="'+escapeHtml(smokeMeta[0])+'">'+escapeHtml(smokeMeta[1])+'</button>'
      : '';
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
      '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn" data-capy-action="createSpaceFromSession">Create from current chat</button><button type="button" class="capy-spaces-btn" data-capy-action="runWeatherWalkthrough">Run weather walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="runNotesWalkthrough">Run notes walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="runKanbanWalkthrough">Run kanban walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="runSnakeWalkthrough">Run snake repair walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="runDashboardWalkthrough">Run dashboard walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="runCameraWalkthrough">Run camera walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="runStockWalkthrough">Run stock walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="runMusicWalkthrough">Run music walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="runProviderSetupWalkthrough">Run provider setup walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="runBigBangWalkthrough">Run Big Bang onboarding</button><button type="button" class="capy-spaces-btn" data-capy-action="runLocalServiceWalkthrough">Run local service walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="runTimeTravelWalkthrough">Run time travel walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="runAdminRecoveryWalkthrough">Run admin recovery walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="runBrowserWalkthrough">Run browser walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="runResearchWalkthrough">Run research walkthrough</button><button type="button" class="capy-spaces-btn" data-capy-action="installWeatherTemplate">Install weather demo</button><button type="button" class="capy-spaces-btn" data-capy-action="installResearchTemplate">Install research harness</button><button type="button" class="capy-spaces-btn" data-capy-action="installDashboardTemplate">Install dashboard demo</button><button type="button" class="capy-spaces-btn" data-capy-action="installCameraTemplate">Install camera dashboard</button><button type="button" class="capy-spaces-btn" data-capy-action="installKanbanTemplate">Install kanban board</button><button type="button" class="capy-spaces-btn" data-capy-action="installNotesTemplate">Install notes app</button><button type="button" class="capy-spaces-btn" data-capy-action="installBrowserTemplate">Install browser surface</button><button type="button" class="capy-spaces-btn" data-capy-action="installStockTemplate">Install stock chart</button><button type="button" class="capy-spaces-btn" data-capy-action="installServiceTemplate">Install local service dashboard</button><button type="button" class="capy-spaces-btn" data-capy-action="installModelSetupTemplate">Install model setup</button><button type="button" class="capy-spaces-btn" data-capy-action="installGameTemplate">Install game sandbox</button><button type="button" class="capy-spaces-btn" data-capy-action="installMusicTemplate">Install music sequencer</button><button type="button" class="capy-spaces-btn" data-capy-action="installBigBangTemplate">Install Big Bang onboarding</button><button type="button" class="capy-spaces-btn" data-capy-action="reloadSpaces">Refresh</button><button type="button" class="capy-spaces-btn" data-capy-action="newSpace">New space</button></div></div>' +
      renderDemoSmokeRunner(demos || []) + renderTrustedSystemWidgets(activeSpaceId) + cards + renderCreatorLoopForm() + renderSpaceAgentImportForm() + renderSpaceForm();
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
    const chatStep = chatAnswer ? 'Chat answer '+chatAnswer : 'Chat answer not recorded';
    const widgetStep = flow.widget_created ? 'Widget created from request' : 'Widget not created from request';
    const reloadStep = flow.reload_verified ? 'Persistent widget verified after reload' : 'Persistent widget not verified after reload';
    const checklist = '<div class="capy-spaces-card"><strong>Weather demo checklist</strong>' +
      '<ol><li>'+escapeHtml(chatStep)+'</li><li>'+escapeHtml(widgetStep)+'</li><li>'+escapeHtml(reloadStep)+'</li></ol></div>';
    return '<div class="capy-spaces-card capy-spaces-demo-flow"><h4>Prompt → widget flow</h4>' +
      checklist +
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
    const notesPreview = renderNotesSmokePreview(notesArtifact, data && data.notes_flow);
    const kanbanBoard = data && data.kanban_board && typeof data.kanban_board === 'object' && !Array.isArray(data.kanban_board)
      ? data.kanban_board
      : {};
    const kanbanPreview = renderKanbanSmokePreview(kanbanBoard);
    const snakeRepairFlow = data && data.snake_repair_flow && typeof data.snake_repair_flow === 'object' && !Array.isArray(data.snake_repair_flow)
      ? data.snake_repair_flow
      : {};
    const snakePreview = renderSnakeRepairPreview(snakeRepairFlow);
    const stockSnapshot = data && data.stock_snapshot && typeof data.stock_snapshot === 'object' && !Array.isArray(data.stock_snapshot)
      ? data.stock_snapshot
      : {};
    const stockPreview = renderStockSmokePreview(stockSnapshot);
    const musicFlow = data && data.music_flow && typeof data.music_flow === 'object' && !Array.isArray(data.music_flow)
      ? data.music_flow
      : {};
    const musicPreview = renderMusicSmokePreview(musicFlow);
    const demoSpaceId = space.space_id ? String(space.space_id) : '';
    const hasNotesPreview = !!notesPreview;
    const hasKanbanPreview = !!kanbanPreview;
    const hasSnakePreview = !!snakePreview;
    const hasStockPreview = !!stockPreview;
    const hasMusicPreview = !!musicPreview;
    const demoTemplate = String(data && data.template || '').toLowerCase();
    const isServiceDemo = demoTemplate === 'service' || demo === 'demo_local_agent_control_dashboard';
    const isProviderSetupDemo = demoTemplate === 'model-setup' || demo === 'demo_provider_setup';
    const manageLabel = weatherPreview ? 'Manage weather widget' : (hasNotesPreview ? 'Manage notes widgets' : (hasKanbanPreview ? 'Manage kanban widgets' : (hasSnakePreview ? 'Manage game widgets' : (hasStockPreview ? 'Manage stock widgets' : (hasMusicPreview ? 'Manage music widgets' : (isProviderSetupDemo ? 'Manage provider widgets' : (isServiceDemo ? 'Manage service widgets' : 'Manage demo widgets')))))));
    const demoActions = demoSpaceId
      ? '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn" data-capy-action="openSpace" data-space-id="'+escapeHtml(demoSpaceId)+'">Open demo Space</button><button type="button" class="capy-spaces-btn" data-capy-action="loadWidgets" data-space-id="'+escapeHtml(demoSpaceId)+'">'+escapeHtml(manageLabel)+'</button></div>'
      : '';
    return '<div class="capy-spaces-card" role="status"><h3>Demo parity smoke passed</h3>' +
      '<div class="capy-spaces-muted">'+escapeHtml(demo)+' · '+escapeHtml(data && data.mode || 'metadata-only-smoke')+'</div>' +
      '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div><strong>'+escapeHtml(spaceName)+'</strong>' +
      '<div class="capy-spaces-muted">Space ID: '+escapeHtml(space.space_id || '')+' · Widgets: '+widgetCount+' · Persisted widgets: '+persistedWidgetCount+' · Persistence: '+escapeHtml(persistence)+' · Revisions: '+revisionCount+' · Rollback point: '+escapeHtml(rollbackPoint)+'</div>' +
      extraLine + '</div>'+demoActions+'</div></div>'+weatherPreview+promptFlowPreview+notesPreview+kanbanPreview+snakePreview+stockPreview+musicPreview+'</div>';
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
      const flow = item && item.prompt_flow && typeof item.prompt_flow === 'object' && !Array.isArray(item.prompt_flow) ? item.prompt_flow : null;
      const flowSummary = demo === 'demo_weather_widget' && flow
        ? '<div class="capy-spaces-muted"><strong>Weather demo checklist</strong></div><div class="capy-spaces-muted">Weather flow: chat answer '+escapeHtml(flow.chat_answer_status ? String(flow.chat_answer_status) : 'not recorded')+' · widget '+(flow.widget_created ? 'created' : 'not created')+' · reload '+(flow.reload_verified ? 'verified' : 'not verified')+'</div>'
        : '';
      const weatherWidget = item && item.weather_observation && item.weather_observation.widget && typeof item.weather_observation.widget === 'object' && !Array.isArray(item.weather_observation.widget)
        ? item.weather_observation.widget
        : null;
      const weatherMeta = weatherWidget && weatherWidget.metadata && weatherWidget.metadata.weather && typeof weatherWidget.metadata.weather === 'object' && !Array.isArray(weatherWidget.metadata.weather)
        ? weatherWidget.metadata.weather
        : null;
      const weatherCurrent = weatherMeta && weatherMeta.current && typeof weatherMeta.current === 'object' && !Array.isArray(weatherMeta.current)
        ? weatherMeta.current
        : {};
      const weatherLocation = weatherMeta ? [weatherMeta.location, weatherMeta.country].filter(Boolean).map(String).join(', ') : '';
      const weatherTemp = weatherCurrent && weatherCurrent.temperature_c !== undefined && weatherCurrent.temperature_c !== null && String(weatherCurrent.temperature_c) !== ''
        ? String(weatherCurrent.temperature_c)+' °C'
        : '';
      const weatherCondition = weatherCurrent && weatherCurrent.condition ? String(weatherCurrent.condition) : '';
      const weatherBridgeCount = Number(item && item.queued_event_count || 0);
      const weatherObservationSummary = demo === 'demo_weather_widget' && weatherMeta
        ? '<div class="capy-spaces-muted">Weather observation: '+[weatherLocation, weatherTemp, weatherCondition].filter(Boolean).map(escapeHtml).join(' · ')+(weatherBridgeCount ? ' · Agent bridge: '+weatherBridgeCount+' queued' : '')+'</div>'
        : '';
      const notesFlow = item && item.notes_flow && typeof item.notes_flow === 'object' && !Array.isArray(item.notes_flow) ? item.notes_flow : null;
      const notesSummary = demo === 'demo_notes_app' && notesFlow
        ? '<div class="capy-spaces-muted"><strong>Notes app checklist</strong></div><div class="capy-spaces-muted">Notes flow: folders '+Number(notesFlow.folder_count || 0)+' · active '+escapeHtml(notesFlow.active_folder ? String(notesFlow.active_folder) : 'none')+' · editor '+(notesFlow.editor_saved ? 'saved' : 'not saved')+' · markdown '+(notesFlow.markdown_preview_saved ? 'saved' : 'not saved')+' · attachments '+(notesFlow.attachments_agent_mediated ? 'agent-mediated' : 'not ready')+'</div>'
        : '';
      const kanbanSuiteBoard = item && item.kanban_board && typeof item.kanban_board === 'object' && !Array.isArray(item.kanban_board) ? item.kanban_board : null;
      const kanbanSuiteColumns = kanbanSuiteBoard && Array.isArray(kanbanSuiteBoard.columns) ? kanbanSuiteBoard.columns : [];
      const kanbanCardCount = kanbanSuiteColumns.reduce(function(count, column){
        const meta = column && column.metadata && typeof column.metadata === 'object' && !Array.isArray(column.metadata) ? column.metadata : {};
        const kanban = meta.kanban && typeof meta.kanban === 'object' && !Array.isArray(meta.kanban) ? meta.kanban : {};
        const cards = Array.isArray(kanban.cards) ? kanban.cards : [];
        return count + cards.length;
      }, 0);
      const kanbanDragPlanned = kanbanSuiteColumns.some(function(column){
        const meta = column && column.metadata && typeof column.metadata === 'object' && !Array.isArray(column.metadata) ? column.metadata : {};
        const kanban = meta.kanban && typeof meta.kanban === 'object' && !Array.isArray(meta.kanban) ? meta.kanban : {};
        const interaction = kanban.interaction && typeof kanban.interaction === 'object' && !Array.isArray(kanban.interaction) ? kanban.interaction : {};
        return interaction.drag_drop === 'planned';
      });
      const kanbanEditsMetadataOnly = kanbanSuiteColumns.some(function(column){
        const meta = column && column.metadata && typeof column.metadata === 'object' && !Array.isArray(column.metadata) ? column.metadata : {};
        const kanban = meta.kanban && typeof meta.kanban === 'object' && !Array.isArray(meta.kanban) ? meta.kanban : {};
        const interaction = kanban.interaction && typeof kanban.interaction === 'object' && !Array.isArray(kanban.interaction) ? kanban.interaction : {};
        return interaction.edit_cards === 'metadata-only';
      });
      const kanbanSummary = demo === 'demo_kanban_board' && kanbanSuiteBoard
        ? '<div class="capy-spaces-muted"><strong>Kanban board checklist</strong></div><div class="capy-spaces-muted">Kanban flow: columns '+Number(kanbanSuiteBoard.column_count || kanbanSuiteColumns.length || 0)+' · cards '+kanbanCardCount+' · drag/drop '+(kanbanDragPlanned ? 'planned' : 'not ready')+' · card edits '+(kanbanEditsMetadataOnly ? 'metadata-only' : 'not ready')+'</div>'
        : '';
      const researchRollback = item && item.research_rollback_check && typeof item.research_rollback_check === 'object' && !Array.isArray(item.research_rollback_check) ? item.research_rollback_check : null;
      const researchQueued = Number(item && item.queued_event_count || 0) > 0;
      const researchSummary = demo === 'demo_research_harness_pdf_export' && researchRollback
        ? '<div class="capy-spaces-muted"><strong>Research harness checklist</strong></div><div class="capy-spaces-muted">Research flow: PDF export '+(researchQueued ? 'queued' : 'not queued')+' · rollback '+(researchRollback.verified === true ? 'verified' : 'not verified')+' · '+(researchRollback.replayed_after_restore === true ? 'replayed after restore' : 'not replayed after restore')+' · restored widgets '+Number(researchRollback.restored_widget_count || 0)+'</div>'
        : '';
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(demo)+'</strong>' +
        '<div class="capy-spaces-muted">template: '+escapeHtml(template)+' · widgets: '+widgetCount+' · persisted: '+persistedWidgetCount+' · persistence: '+escapeHtml(persistence)+' · rollback point: '+escapeHtml(rollbackPoint)+'</div>' + flowSummary + weatherObservationSummary + notesSummary + kanbanSummary + researchSummary + '</div></div>';
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

  function renderCreatorLoopForm(){
    return '<div class="capy-spaces-card"><h3>Safe creator loop</h3>' +
      '<div class="capy-spaces-muted">Prompt → bounded metadata spec → sandbox preview → visual QA → revisioned commit. The preview and commit cards show metadata summaries only.</div>' +
      '<div class="capy-spaces-form" aria-label="Preview safe creator loop">' +
      '<label>Creator prompt<textarea id="capyCreatorPrompt" rows="4" autocomplete="off" placeholder="Describe a workspace tool to draft safely"></textarea></label>' +
      '<label>Target existing Space ID (optional)<input id="capyCreatorTargetSpaceId" autocomplete="off" placeholder="existing-space-id"></label>' +
      '<button type="button" class="capy-spaces-btn" data-capy-action="previewCreatorSpec">Preview bounded spec</button>' +
      '</div></div>';
  }

  function safeCreatorSummaryText(value){
    const unsafeValuePattern = /(api[_-]?(key|auth)|apiauth|apikey|authorization|bearer|cookie|credential|credentials|password|secret|token|<script|<\/script|javascript:|onerror|onload|renderer|raw_prompt|generated_code|source|(?:^|[_\-\s<>])(?:html|script|data)(?:$|[_\-\s<>]))/i;
    const text = String(value == null ? '' : value).replace(/\s+/g, ' ').trim().slice(0, 120);
    return text && !unsafeValuePattern.test(text) ? text : '';
  }

  function safeCreatorIdText(value){
    const text = safeCreatorSummaryText(value);
    return /^[a-z0-9][a-z0-9_-]{0,80}$/i.test(text) ? text : '';
  }

  function safeDisplayMetadataText(value, fallback){
    const text = String(value == null ? '' : value).replace(/\s+/g, ' ').trim().slice(0, 120);
    if (!text) return fallback || '';
    const unsafeDisplayPattern = /(api[_-]?(key|auth)|apiauth|apikey|authorization|bearer\s+[^\s,;]+|cookie\s*[:=]|credential|credentials|password|secret(?:[_-][a-z0-9_-]+|\b)|token\s*[:=]|<script|<\/script|javascript:|onerror|onload|renderer|generated[ _-]?code|raw[ _-]?prompt)/i;
    return unsafeDisplayPattern.test(text) ? '[REDACTED]' : text;
  }

  function renderCreatorSpecSummary(spec){
    const safeSpec = spec && typeof spec === 'object' && !Array.isArray(spec) ? spec : {};
    const space = safeSpec.space && typeof safeSpec.space === 'object' && !Array.isArray(safeSpec.space) ? safeSpec.space : {};
    const spaceName = safeCreatorSummaryText(space.name) || safeCreatorIdText(space.space_id || '') || 'Draft Space';
    const spaceId = safeCreatorIdText(space.space_id || '');
    const widgets = Array.isArray(safeSpec.widgets) ? safeSpec.widgets.slice(0, 6) : [];
    const widgetRows = widgets.map(function(widget){
      if (!widget || typeof widget !== 'object' || Array.isArray(widget)) return '';
      const widgetId = safeCreatorIdText(widget.id || '');
      const title = safeCreatorSummaryText(widget.title || widgetId || 'Draft widget') || 'Draft widget';
      const kind = safeCreatorSummaryText(widget.kind || 'custom') || 'custom';
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(title)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(kind)+(widgetId ? ' · '+escapeHtml(widgetId) : '')+'</div></div></div>';
    }).filter(Boolean).join('') || '<div class="capy-spaces-muted">No widget metadata returned.</div>';
    return '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div><strong>'+escapeHtml(spaceName)+'</strong>' +
      (spaceId ? '<div class="capy-spaces-muted">Space ID: '+escapeHtml(spaceId)+'</div>' : '') +
      '</div></div>'+widgetRows+'</div>';
  }

  function renderCreatorRevisionPreview(data){
    const preview = data && data.revision_preview && typeof data.revision_preview === 'object' && !Array.isArray(data.revision_preview) ? data.revision_preview : null;
    const diff = data && data.revision_diff && typeof data.revision_diff === 'object' && !Array.isArray(data.revision_diff) ? data.revision_diff : null;
    if (!preview && !diff) return '';
    const space = data && data.space && typeof data.space === 'object' && !Array.isArray(data.space) ? data.space : {};
    const spaceId = safeCreatorIdText((preview && preview.space_id) || space.space_id || '');
    const spaceName = safeCreatorSummaryText((preview && preview.name) || space.name || spaceId || 'Target Space') || 'Target Space';
    const previewText = formatRestorePreview(preview);
    const diffText = formatRestoreDiff(diff);
    return '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div><strong>Revision preview</strong>' +
      '<div class="capy-spaces-muted">'+escapeHtml(spaceName)+(spaceId ? ' · Space ID: '+escapeHtml(spaceId) : '')+'</div>' +
      (previewText ? '<div class="capy-spaces-muted">'+escapeHtml(previewText)+'</div>' : '') +
      (diffText ? '<div class="capy-spaces-muted">'+escapeHtml(diffText)+'</div>' : '') +
      '</div></div></div>';
  }

  function renderCreatorPreviewResult(data){
    const previewId = safeCreatorIdText(data && data.preview_id || '');
    const stage = safeCreatorSummaryText(data && data.stage || 'sandbox-preview-required') || 'sandbox-preview-required';
    const stored = data && data.stored === true ? 'true' : 'false';
    const executed = data && data.executed === true ? 'true' : 'false';
    const gates = data && data.gates && typeof data.gates === 'object' && !Array.isArray(data.gates) ? data.gates : {};
    const gateLabels = [];
    if (gates.sandbox_preview_required) gateLabels.push('sandbox preview required');
    if (gates.visual_qa_required) gateLabels.push('visual QA required');
    if (gates.approve_commit_required) gateLabels.push('approval required');
    const commitButton = previewId ? '<div class="capy-spaces-actions"><button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="commitCreatorSpec" data-preview-id="'+escapeHtml(previewId)+'">Approve revisioned commit</button></div>' : '';
    return '<div class="capy-spaces-card" role="status"><h3>Creator preview ready</h3>' +
      '<div class="capy-spaces-muted">'+escapeHtml(stage)+' · stored: '+stored+' · executed: '+executed+(gateLabels.length ? ' · '+escapeHtml(gateLabels.join(' · ')) : '')+'</div>' +
      renderCreatorSpecSummary(data && data.spec) + renderCreatorRevisionPreview(data || {}) + commitButton + '</div>';
  }

  function renderCreatorCommitResult(data){
    const space = data && data.space && typeof data.space === 'object' && !Array.isArray(data.space) ? data.space : {};
    const spaceName = safeCreatorSummaryText(space.name || space.space_id || 'Committed Space') || 'Committed Space';
    const spaceId = safeCreatorIdText(space.space_id || '');
    const rev = safeCreatorSummaryText(space.revision_event_id || data && data.revision_event && data.revision_event.event_id || '');
    const stage = safeCreatorSummaryText(data && data.stage || 'revisioned-commit') || 'revisioned-commit';
    const stored = data && data.stored === true ? 'true' : 'false';
    const executed = data && data.executed === true ? 'true' : 'false';
    const revisionReceipt = renderCreatorRevisionPreview(data || {});
    const actions = spaceId ? '<div class="capy-spaces-actions">' +
      '<button type="button" class="capy-spaces-btn" data-capy-action="openSpace" data-space-id="'+escapeHtml(spaceId)+'">Open committed Space</button>' +
      '<button type="button" class="capy-spaces-btn" data-capy-action="loadWidgets" data-space-id="'+escapeHtml(spaceId)+'">Manage committed widgets</button>' +
      '</div>' : '';
    return '<div class="capy-spaces-card" role="status"><h3>Creator commit saved</h3>' +
      '<div class="capy-spaces-muted">'+escapeHtml(stage)+' · stored: '+stored+' · executed: '+executed+(rev ? ' · Revision: '+escapeHtml(rev) : '')+'</div>' +
      revisionReceipt +
      '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div><strong>'+escapeHtml(spaceName)+'</strong>' +
      (spaceId ? '<div class="capy-spaces-muted">Space ID: '+escapeHtml(spaceId)+'</div>' : '') +
      '</div>'+actions+'</div></div></div>';
  }

  function renderCreatorCommitBlockedResult(){
    return '<div class="capy-spaces-card capy-spaces-danger-card" role="status"><h3>Creator commit blocked</h3>' +
      '<div class="capy-spaces-muted">Preview expired or target changed; refresh preview before committing.</div>' +
      '</div>';
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
      const title = safeDisplayMetadataText(w.title, widgetId || 'Untitled widget') || widgetId || 'Untitled widget';
      const kind = safeDisplayMetadataText(w.kind, 'custom') || 'custom';
      return '<div class="capy-spaces-widget" data-widget-id="'+escapeHtml(widgetId)+'"><div><strong>'+escapeHtml(title)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(kind)+' · '+escapeHtml(widgetId)+' · '+escapeHtml(formatWidgetLayout(layout))+'</div></div></div>';
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
    const unsafeParts = ['renderer', 'html', 'script', 'data', 'source', 'api_key', 'api_auth', 'apiauth', 'apikey', 'token', 'password', 'secret', 'credential', 'credentials', 'cookie', 'authorization'];
    const unsafeValuePattern = /(api[_-]?(key|auth)|apiauth|apikey|authorization|bearer|cookie|credential|credentials|password|secret|token|<script|<\/script|javascript:|onerror|onload|renderer|source|(?:^|[_\-\s<>])(?:html|script|data)(?:$|[_\-\s<>]))/i;
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

  function safeExportMetadataText(value, fallback){
    const text = String(value == null ? '' : value).replace(/\s+/g, ' ').trim().slice(0, 160);
    if (!text) return fallback || '';
    const unsafePattern = /(api[_-]?(key|auth)|apiauth|apikey|authorization|bearer\b|cookie\b|credential|credentials|password|secret(?:[_-]?[a-z0-9_-]+|\b)|token\b|<script|<\/script|javascript:|onerror|onload|renderer|generated[ _-]?code|raw[ _-]?prompt|space[_-]?yaml|archive[_-]?b64|zip[_-]?b64|base64|widgets[\\\/][^\s<>]*\.ya?ml\b|(?:source|html|script|data)(?:code|panel|widget|source|body|text)|(?:^|[._\-/\s;:@<>])(?:source|html|script|data)(?:$|[._\-/\s;:@<>]))/i;
    return unsafePattern.test(text) ? (fallback || '') : text;
  }

  function renderSpaceExportResult(spaceId, data){
    const rawFormat = data && data.format ? String(data.format) : 'yaml';
    const format = rawFormat.indexOf('zip') >= 0 ? 'zip' : 'yaml';
    const safeSpaceId = safeExportMetadataText(spaceId || (data && data.space_id) || '', 'redacted-export') || 'redacted-export';
    const fallbackFilename = safeSpaceId + '-space-agent.' + (format === 'zip' ? 'zip' : 'yaml');
    const filename = safeExportMetadataText(data && data.filename ? String(data.filename) : fallbackFilename, fallbackFilename) || fallbackFilename;
    const widgetCount = data && Number.isFinite(Number(data.widget_count)) ? Number(data.widget_count) : 0;
    return '<div class="capy-spaces-card"><h3>Space Agent export ready</h3>' +
      '<div class="capy-spaces-muted">Safe metadata package generated. Package contents are intentionally not displayed in this UI.</div>' +
      '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div><strong>'+escapeHtml(filename)+'</strong>' +
      '<div class="capy-spaces-muted">Format: '+escapeHtml(format)+' · Space ID: '+escapeHtml(safeSpaceId)+' · Widgets: '+widgetCount+'</div></div></div></div></div>';
  }

  function safeImportMetadataText(value, fallback){
    const text = String(value == null ? '' : value).replace(/\s+/g, ' ').trim().slice(0, 120);
    if (!text) return fallback || '';
    const unsafePattern = /(api[_-]?(key|auth)|apiauth|apikey|authorization|bearer\b|cookie\b|credential|credentials|password|secret(?:[_-][a-z0-9_-]+|\b)|token\b|<script|<\/script|javascript:|onerror|onload|renderer|generated[ _-]?code|raw[ _-]?prompt|(?:^|[._\-/\s;:@<>])(?:source|html|script|data)(?:$|[._\-/\s;:@<>]))/i;
    return unsafePattern.test(text) ? (fallback || '') : text;
  }

  function renderSpaceImportResult(data){
    const space = data && data.space && typeof data.space === 'object' ? data.space : {};
    const spaceId = safeImportMetadataText(space.space_id || data && data.space_id || '', '');
    const name = safeImportMetadataText(space.name || '', spaceId || 'Imported space') || spaceId || 'Imported space';
    const widgets = Array.isArray(data && data.imported_widgets) ? data.imported_widgets : [];
    const widgetRows = widgets.length ? widgets.map(function(w){
      const widgetId = safeImportMetadataText(w && w.id ? String(w.id) : '', '');
      const title = safeImportMetadataText(w && w.title ? String(w.title) : '', widgetId || 'Untitled widget') || widgetId || 'Untitled widget';
      const kind = safeImportMetadataText(w && w.kind ? String(w.kind) : (w && w.type ? String(w.type) : ''), 'custom') || 'custom';
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(title)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(kind)+(widgetId ? ' · '+escapeHtml(widgetId) : '')+'</div></div></div>';
    }).join('') : '<div class="capy-spaces-muted">No imported widget metadata returned.</div>';
    const count = widgets.length;
    const warnings = Array.isArray(data && data.warnings) ? data.warnings : [];
    const warningRows = warnings.length ? '<div class="capy-spaces-card"><h3>Import warnings</h3>' +
      '<div class="capy-spaces-muted">Unsupported Space Agent API calls were not imported. Recreate them through safe Capy tools after review.</div>' +
      '<div class="capy-spaces-widget-list">' + warnings.slice(0, 8).map(function(w){
        const api = safeImportMetadataText(w && w.api ? String(w.api) : '', 'unsupported API') || 'unsupported API';
        const message = safeImportMetadataText(w && w.message ? String(w.message) : '', 'Space Agent package warning omitted pending sandbox review.') || 'Space Agent package warning omitted pending sandbox review.';
        return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(api)+'</strong>' +
          '<div class="capy-spaces-muted">'+escapeHtml(message)+'</div></div></div>';
      }).join('') + '</div></div>' : '';
    return '<div class="capy-spaces-card"><h3>Space Agent import ready</h3>' +
      '<div class="capy-spaces-muted">Imported package metadata only. Generated widget bodies remain quarantined/disabled for review by the backend.</div>' +
      '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div><strong>'+escapeHtml(name)+'</strong>' +
      '<div class="capy-spaces-muted">Space ID: '+escapeHtml(spaceId || 'redacted-import')+' · '+count+' widget'+(count === 1 ? '' : 's')+'</div></div></div>'+widgetRows+'</div></div>' + warningRows;
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
      const diffText = formatRestoreDiff(rev && rev.restore_diff);
      const widgetRestoreButtons = renderRestoreWidgetButtons(spaceId, eventId, rev && rev.restore_diff);
      const restoreButton = eventId ? '<button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="restoreRevision" data-space-id="'+escapeHtml(spaceId || '')+'" data-event-id="'+escapeHtml(eventId)+'">Restore</button>' : '';
      const actions = (restoreButton || widgetRestoreButtons) ? '<div class="capy-spaces-actions">'+restoreButton+widgetRestoreButtons+'</div>' : '';
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(eventType)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(formatRevisionTime(rev && rev.created_at))+' · '+escapeHtml(eventId.slice(0, 12) || 'no-event-id')+'</div>' +
        (detailText ? '<div class="capy-spaces-muted">'+escapeHtml(detailText)+'</div>' : '') +
        (previewText ? '<div class="capy-spaces-muted">'+escapeHtml(previewText)+'</div>' : '') +
        (diffText ? '<div class="capy-spaces-muted">'+escapeHtml(diffText)+'</div>' : '') +
        '</div>'+actions+'</div>';
    }).join('') : '<div class="capy-spaces-muted">No revision events recorded yet.</div>';
    return '<div class="capy-spaces-card"><h3>Revision history</h3>' +
      '<div class="capy-spaces-muted">Newest safe metadata events. Restore rewrites the Space manifest from a stored snapshot; generated widget bodies are not displayed.</div>' +
      '<div class="capy-spaces-widget-list">'+rows+'</div></div>';
  }

  function formatRevisionDetails(details){
    if (!details || typeof details !== 'object' || Array.isArray(details)) return '';
    const unsafeParts = ['renderer', 'html', 'script', 'data', 'source', 'api_key', 'api_auth', 'apiauth', 'apikey', 'token', 'password', 'secret', 'credential', 'credentials', 'cookie', 'authorization'];
    const unsafeValuePattern = /(api[_-]?(key|auth)|apiauth|apikey|authorization|bearer|cookie|credential|credentials|password|secret|token|<script|<\/script|javascript:|onerror|onload|renderer|source|(?:^|[_\-\s<>])(?:html|script|data)(?:$|[_\-\s<>]))/i;
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
    const unsafeValuePattern = /(api[_-]?(key|auth)|apiauth|apikey|authorization|bearer|cookie|credential|credentials|password|secret|token|<script|<\/script|javascript:|onerror|onload|renderer|raw_prompt|generated_code|source|(?:^|[_\-\s<>])(?:script|data)(?:$|[_\-\s<>]))/i;
    function safePreviewText(value){
      const text = String(value || '').replace(/\s+/g, ' ').trim().slice(0, 80);
      return text && !unsafeValuePattern.test(text) ? text : '';
    }
    const name = safePreviewText(preview.name) || safeCreatorIdText(preview.space_id || '') || 'unnamed snapshot';
    const count = Number(preview.widget_count || 0);
    const countLabel = count === 1 ? '1 widget' : count+' widgets';
    const widgets = Array.isArray(preview.widgets) ? preview.widgets.slice(0, 5).map(function(widget){
      if (!widget || typeof widget !== 'object' || Array.isArray(widget)) return '';
      return [safeCreatorIdText(widget.id || ''), safePreviewText(widget.title), safePreviewText(widget.kind)].filter(Boolean).join(' / ');
    }).filter(Boolean) : [];
    return 'Preview: '+name+' · '+countLabel+(widgets.length ? ' · Widgets: '+widgets.join(', ') : '');
  }

  function formatRestoreDiff(diff){
    if (!diff || typeof diff !== 'object' || Array.isArray(diff) || !diff.has_changes) return '';
    const unsafeValuePattern = /(api[_-]?(key|auth)|apiauth|apikey|authorization|bearer|cookie|credential|credentials|password|secret|token|<script|<\/script|javascript:|onerror|onload|renderer|source|(?:^|[_\-\s<>])(?:html|script|data)(?:$|[_\-\s<>]))/i;
    function safeList(value){
      if (!Array.isArray(value)) return [];
      return value.slice(0, 10).map(function(item){
        const text = String(item || '').replace(/\s+/g, ' ').trim().slice(0, 80);
        if (text === 'shared_data') return text;
        return text && !unsafeValuePattern.test(text) ? text : '';
      }).filter(Boolean);
    }
    function safeIdList(value){
      if (!Array.isArray(value)) return [];
      return value.slice(0, 10).map(function(item){
        return safeCreatorIdText(item || '');
      }).filter(Boolean);
    }
    function plural(count, singular){
      return count+' '+singular+(count === 1 ? '' : 's');
    }
    const fields = safeList(diff.space_fields_to_update);
    const addWidgets = safeIdList(diff.widgets_to_add);
    const removeWidgets = safeIdList(diff.widgets_to_remove);
    const updateWidgets = safeIdList(diff.widgets_to_update);
    const summary = [];
    if (fields.length) summary.push('changes '+plural(fields.length, 'field'));
    if (addWidgets.length) summary.push('adds '+plural(addWidgets.length, 'widget'));
    if (removeWidgets.length) summary.push('removes '+plural(removeWidgets.length, 'widget'));
    if (updateWidgets.length) summary.push('updates '+plural(updateWidgets.length, 'widget'));
    const details = [];
    if (fields.length) details.push('Fields: '+fields.join(', '));
    if (addWidgets.length) details.push('Add widgets: '+addWidgets.join(', '));
    if (removeWidgets.length) details.push('Remove widgets: '+removeWidgets.join(', '));
    if (updateWidgets.length) details.push('Update widgets: '+updateWidgets.join(', '));
    if (!summary.length) return '';
    return 'Diff: restore '+summary.join(', ')+(details.length ? ' · '+details.join(' · ') : '');
  }

  function safeRestoreWidgetIds(diff){
    if (!diff || typeof diff !== 'object' || Array.isArray(diff) || !diff.has_changes) return [];
    const unsafeValuePattern = /(api[_-]?(key|auth)|apiauth|apikey|authorization|bearer|cookie|credential|credentials|password|secret|token|<script|<\/script|javascript:|onerror|onload|renderer|source|(?:^|[_\-\s<>])(?:html|script|data)(?:$|[_\-\s<>]))/i;
    const ids = [];
    [diff.widgets_to_update, diff.widgets_to_add].forEach(function(list){
      if (!Array.isArray(list)) return;
      list.slice(0, 10).forEach(function(item){
        const text = String(item || '').replace(/\s+/g, ' ').trim().slice(0, 80);
        const safeId = safeCreatorIdText(text);
        if (safeId && !unsafeValuePattern.test(safeId) && ids.indexOf(safeId) === -1) ids.push(safeId);
      });
    });
    return ids;
  }

  function renderRestoreWidgetButtons(spaceId, eventId, diff, actionName){
    if (!spaceId || !eventId) return '';
    const action = actionName || 'restoreWidgetRevision';
    return safeRestoreWidgetIds(diff).map(function(widgetId){
      return '<button type="button" class="capy-spaces-btn" data-capy-action="'+escapeHtml(action)+'" data-space-id="'+escapeHtml(spaceId)+'" data-event-id="'+escapeHtml(eventId)+'" data-widget-id="'+escapeHtml(widgetId)+'">Restore widget</button>';
    }).join('');
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

  function renderWidgetRecoveryStatus(widget){
    const recovery = widget && widget.recovery && typeof widget.recovery === 'object' && !Array.isArray(widget.recovery) ? widget.recovery : {};
    if (!recovery.disabled) return '';
    const rawReason = recovery.disabled_reason ? String(recovery.disabled_reason).replace(/\s+/g, ' ').trim().slice(0, 160) : '';
    const safeReason = rawReason ? ' · '+safeDisplayMetadataText(rawReason, '[REDACTED]') : '';
    return '<div class="capy-spaces-muted">Recovery: disabled'+escapeHtml(safeReason)+'</div>';
  }

  function renderWidgetManager(spaceId, widgets, events){
    const widgetCards = widgets.length ? widgets.map(function(w){
      const widgetId = w.id || '';
      const title = safeDisplayMetadataText(w.title, widgetId || 'Untitled widget') || widgetId || 'Untitled widget';
      const kind = safeDisplayMetadataText(w.kind, 'custom') || 'custom';
      const layout = widgetLayout(w);
      return '<div class="capy-spaces-widget" data-widget-id="'+escapeHtml(widgetId)+'">' +
        '<div><strong>'+escapeHtml(title)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(kind)+' · '+escapeHtml(widgetId)+' · '+escapeHtml(formatWidgetLayout(layout))+'</div>' +
        renderWidgetRecoveryStatus(w) +
        renderWidgetPrompt(w.metadata || {}) + renderWeatherObservation(w.metadata || {}) + renderWidgetAgentBridgeStatus(widgetId, events || []) + '</div>' +
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

  function runtimeTokenPart(value, fallback){
    const text = String(value || fallback || '').replace(/[^a-z0-9._-]/gi, '-').replace(/-+/g, '-').slice(0, 80);
    return text || String(fallback || 'runtime');
  }

  function generateRuntimeToken(){
    const randomPart = (typeof crypto !== 'undefined' && crypto.getRandomValues)
      ? Array.from(crypto.getRandomValues(new Uint32Array(2))).map(function(n){ return n.toString(36); }).join('-')
      : Math.random().toString(36).slice(2) + '-' + Date.now().toString(36);
    return 'capy-runtime-' + randomPart;
  }

  function registerWidgetRuntimeSession(spaceId, widgetId, title){
    const safeSpaceId = runtimeTokenPart(spaceId, 'space');
    const safeWidgetId = runtimeTokenPart(widgetId, 'widget');
    const key = safeSpaceId + '::' + safeWidgetId;
    const existing = widgetRuntimeSessions[key];
    if (existing && existing.token) delete widgetRuntimeSessions[existing.token];
    const session = { token: generateRuntimeToken(), spaceId: safeSpaceId, widgetId: safeWidgetId, title: String(title || widgetId || 'widget').replace(/\s+/g, ' ').trim().slice(0, 120) };
    widgetRuntimeSessions[key] = session;
    widgetRuntimeSessions[session.token] = session;
    return session;
  }

  function redactRuntimeText(value, maxLen){
    let text = String(value || '').replace(/\s+/g, ' ').trim().slice(0, maxLen || 500);
    const unsafeAssignmentMarker = /\b(api[_-]?key|api[_-]?auth|auth|access[_-]?token|refresh[_-]?token|token|authorization|password|credential(?:s)?|cookie|secret|prompt|generated[_-]?code|source|html|data)\b\s*[:=]|javascript\s*:|\bon[a-z]+\s*=/i;
    const standaloneSecretShape = /\b(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[0-9A-Z]{12,})\b/;
    if (unsafeAssignmentMarker.test(text) || standaloneSecretShape.test(text)) return '[REDACTED] sandbox prompt: unsafe markers omitted';
    text = text.replace(/<\/?script[^>]*>/gi, '');
    text = text.replace(/<[^>]*>/g, '');
    text = text.replace(/javascript\s*:/gi, '[REDACTED]');
    text = text.replace(/\bon[a-z]+\s*=\s*[^\s,;]+/gi, '[REDACTED]');
    text = text.replace(/SECRET[_A-Z0-9-]*/gi, '[REDACTED]');
    text = text.replace(/\b(api[_-]?key|api[_-]?auth|auth|access[_-]?token|refresh[_-]?token|token|authorization|password|credential(?:s)?|cookie|secret|prompt|generated[_-]?code|source|html|data)\b\s*[:=]\s*[^\s,;]*/gi, '[REDACTED]');
    text = text.replace(/\b(api[_-]?key|api[_-]?auth|auth|access[_-]?token|refresh[_-]?token|token|authorization|password|credential(?:s)?|cookie)\b\s+[^\s,;]+/gi, '[REDACTED]');
    text = text.replace(/\bbearer\s+[^\s,;]+/gi, '[REDACTED]');
    text = text.replace(standaloneSecretShape, '[REDACTED]');
    return text.replace(/\s+/g, ' ').trim().slice(0, maxLen || 500);
  }

  function runtimeSessionStillVisible(token){
    const root = document.getElementById('capySpacesRoot');
    const safeToken = escapeHtml(String(token || ''));
    return !!(root && typeof root.innerHTML === 'string' && safeToken && root.innerHTML.indexOf('data-runtime-token="'+safeToken+'"') !== -1);
  }

  function renderSandboxRuntimeStatus(title, message){
    return '<div class="capy-spaces-card capy-spaces-runtime-status" role="status"><h3>'+escapeHtml(title)+'</h3>' +
      '<div class="capy-spaces-muted">'+escapeHtml(message || 'Metadata-only sandbox event handled.')+'</div></div>';
  }

  function renderSandboxRuntimeShell(spaceId, widgetId, title){
    if (!spaceId || !widgetId) return '';
    const session = registerWidgetRuntimeSession(spaceId, widgetId, title);
    return '<div class="capy-spaces-widget capy-spaces-sandbox-shell" data-runtime-token="'+escapeHtml(session.token)+'" data-space-id="'+escapeHtml(session.spaceId)+'" data-widget-id="'+escapeHtml(session.widgetId)+'">' +
      '<div><strong>Sandbox event bridge</strong>' +
      '<div class="capy-spaces-muted">postMessage contract: capy:ready, capy:resize, capy:agent:prompt</div>' +
      '<div class="capy-spaces-muted">Generated bodies remain disabled; prompts require approval and metadata-only queueing.</div>' +
      '<div class="capy-spaces-muted">Runtime status: waiting for safe sandbox message</div>' +
      '</div></div>';
  }

  function runtimeMessageTypeValue(value){
    const type = String(value || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    return /^capy:[a-z0-9:._-]+$/i.test(type) ? type : '';
  }

  function runtimeMessageTypeInfo(data){
    const hasType = Object.prototype.hasOwnProperty.call(data || {}, 'type');
    const hasMessageType = Object.prototype.hasOwnProperty.call(data || {}, 'message_type');
    const type = runtimeMessageTypeValue(data && data.type);
    const messageType = runtimeMessageTypeValue(data && data.message_type);
    if ((hasType && !type) || (hasMessageType && !messageType)) return { type: '', blocked: true };
    if (type && messageType && type.toLowerCase() !== messageType.toLowerCase()) return { type: '', blocked: true };
    const selected = type || messageType;
    return { type: selected, blocked: isBlockedRuntimeMessageType(selected) };
  }

  function runtimeMessageType(data){
    return runtimeMessageTypeInfo(data).type;
  }

  function isBlockedRuntimeMessageType(type){
    const text = String(type || '').toLowerCase();
    return text === 'capy:raw:eval' ||
      text === 'capy:asset:url' ||
      /^capy:raw:/.test(text) ||
      /^capy:eval(?::|$)/.test(text) ||
      /^capy:data:(get|put|patch|post|set|delete|remove|merge|write|mutate)$/i.test(text);
  }

  function runtimeMessageOriginAllowed(event){
    // Sandboxed widget shells use an opaque origin; fail closed for normal page/foreign origins.
    return String(event && event.origin || '') === 'null';
  }

  function prependRuntimeStatus(html){
    const root = document.getElementById('capySpacesRoot');
    if (root) root.innerHTML = html + root.innerHTML;
  }

  async function handleCapyWidgetRuntimeMessage(event){
    const data = event && event.data && typeof event.data === 'object' && !Array.isArray(event.data) ? event.data : {};
    const typeInfo = runtimeMessageTypeInfo(data);
    if ((!typeInfo.type && !typeInfo.blocked) || !runtimeMessageOriginAllowed(event)) return;
    const token = String(data.runtime_token || '').trim();
    const session = token ? widgetRuntimeSessions[token] : null;
    if (!session) return;
    if (!runtimeSessionStillVisible(token)) return;
    if (data.space_id && runtimeTokenPart(data.space_id, '') !== session.spaceId) return;
    if (data.widget_id && runtimeTokenPart(data.widget_id, '') !== session.widgetId) return;
    if (typeInfo.blocked) {
      prependRuntimeStatus(renderSandboxRuntimeStatus('Sandbox message blocked', 'Blocked by Capy runtime contract; no widget event was queued.'));
      return;
    }
    const type = typeInfo.type;
    if (type === 'capy:ready') {
      prependRuntimeStatus(renderSandboxRuntimeStatus('Sandbox ready', session.widgetId+' · metadata-only runtime handshake'));
      return;
    }
    if (type === 'capy:resize') {
      const height = Math.max(120, Math.min(900, parseInt(data.height || data.h || 0, 10) || 240));
      prependRuntimeStatus(renderSandboxRuntimeStatus('Sandbox resize noted', session.widgetId+' · bounded height '+height+'px'));
      return;
    }
    if (type !== 'capy:agent:prompt') return;
    if (typeof showConfirmDialog !== 'function') return;
    const promptText = redactRuntimeText(data.prompt || data.message || '', 500);
    if (!promptText) return;
    const ok = await showConfirmDialog({
      title: 'Queue sandbox widget prompt?',
      message: 'Sandbox widget "'+session.title+'" requested a Capy prompt. Preview: '+promptText,
      confirmLabel: 'Queue prompt',
      focusCancel: true,
    });
    if (!ok) return;
    const result = await postSpacesJson('api/spaces/widget/event', {
      space_id: session.spaceId,
      widget_id: session.widgetId,
      event_name: 'agent.prompt',
      prompt: promptText,
      payload: {source: 'sandbox-postmessage', message_type: 'capy:agent:prompt'},
    });
    prependRuntimeStatus(renderSandboxRuntimeStatus('Sandbox prompt queued', 'Widget event queued · '+session.widgetId+' · agent.prompt') + renderWidgetEventQueuedStatus(result || {}));
  }

  function ensureCapyWidgetRuntimeMessageHandler(){
    if (runtimeMessagesBound || typeof window === 'undefined' || !window.addEventListener) return;
    window.addEventListener('message', handleCapyWidgetRuntimeMessage);
    runtimeMessagesBound = true;
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

  function renderNotesFlowChecklist(flow){
    const safeFlow = flow && typeof flow === 'object' && !Array.isArray(flow) ? flow : {};
    if (!Object.keys(safeFlow).length) return '';
    const folderStep = safeFlow.folders_ready ? 'Folder list ready' : 'Folder list pending';
    const editorStep = safeFlow.editor_saved ? 'Editor draft saved' : 'Editor draft pending';
    const previewStep = safeFlow.markdown_preview_saved ? 'Markdown preview saved' : 'Markdown preview pending';
    const attachmentsStep = safeFlow.attachments_agent_mediated ? 'Attachments remain agent-mediated' : 'Attachments pending';
    return '<div class="capy-spaces-card"><strong>Notes app checklist</strong>' +
      '<ol><li>'+escapeHtml(folderStep)+'</li><li>'+escapeHtml(editorStep)+'</li><li>'+escapeHtml(previewStep)+'</li><li>'+escapeHtml(attachmentsStep)+'</li></ol>' +
      '<div class="capy-spaces-muted">Space Agent notes-app path remains metadata-only until richer editors and files are explicitly sandboxed.</div></div>';
  }

  function renderNotesSmokePreview(notesArtifact, flow){
    const artifact = notesArtifact && typeof notesArtifact === 'object' && !Array.isArray(notesArtifact) ? notesArtifact : {};
    const foldersWidget = artifact.folders && typeof artifact.folders === 'object' && !Array.isArray(artifact.folders) ? artifact.folders : {};
    const attachmentsWidget = artifact.attachments && typeof artifact.attachments === 'object' && !Array.isArray(artifact.attachments) ? artifact.attachments : {};
    const editor = artifact.editor && typeof artifact.editor === 'object' && !Array.isArray(artifact.editor) ? artifact.editor : {};
    const preview = artifact.preview && typeof artifact.preview === 'object' && !Array.isArray(artifact.preview) ? artifact.preview : {};
    const folderMeta = foldersWidget.metadata && typeof foldersWidget.metadata === 'object' && !Array.isArray(foldersWidget.metadata) ? foldersWidget.metadata : {};
    const attachmentsMeta = attachmentsWidget.metadata && typeof attachmentsWidget.metadata === 'object' && !Array.isArray(attachmentsWidget.metadata) ? attachmentsWidget.metadata : {};
    const editorMeta = editor.metadata && typeof editor.metadata === 'object' && !Array.isArray(editor.metadata) ? editor.metadata : {};
    const previewMeta = preview.metadata && typeof preview.metadata === 'object' && !Array.isArray(preview.metadata) ? preview.metadata : {};
    const folders = Array.isArray(folderMeta.folders) ? folderMeta.folders : [];
    const attachmentsInfo = attachmentsMeta.attachments && typeof attachmentsMeta.attachments === 'object' && !Array.isArray(attachmentsMeta.attachments) ? attachmentsMeta.attachments : {};
    const attachmentItems = Array.isArray(attachmentsInfo.items) ? attachmentsInfo.items : [];
    const interaction = folderMeta.interaction && typeof folderMeta.interaction === 'object' && !Array.isArray(folderMeta.interaction) ? folderMeta.interaction : {};
    const editorNotes = editorMeta.notes && typeof editorMeta.notes === 'object' && !Array.isArray(editorMeta.notes) ? editorMeta.notes : {};
    const previewNotes = previewMeta.notes && typeof previewMeta.notes === 'object' && !Array.isArray(previewMeta.notes) ? previewMeta.notes : {};
    const status = safeWeatherText(editorNotes.status, 80);
    const editorBody = safeWeatherText(editorNotes.body, 360);
    const previewBody = safeWeatherText(previewNotes.body, 360);
    const format = safeWeatherText(previewNotes.format || editorNotes.format, 40);
    const rows = [];
    const folderRows = folders.slice(0, 8).map(function(folder){
      const title = safeWeatherText(folder && folder.title, 120);
      return title ? '<div class="capy-spaces-muted">• '+escapeHtml(title)+'</div>' : '';
    }).filter(Boolean);
    const folderCount = Number(flow && flow.folder_count) || folderRows.length;
    const activeFolder = safeWeatherText(flow && flow.active_folder, 120);
    const renameMode = safeWeatherText(interaction.rename, 80);
    const createMode = safeWeatherText(interaction.create_folder, 80);
    const folderPreview = folderRows.length ? '<div class="capy-spaces-card"><strong>Folder list preview</strong>' +
      '<div class="capy-spaces-muted">Folders: '+escapeHtml(folderCount)+'</div>' +
      (activeFolder ? '<div class="capy-spaces-muted">Active folder: '+escapeHtml(activeFolder)+'</div>' : '') +
      '<div>'+folderRows.join('')+'</div>' +
      (renameMode ? '<div class="capy-spaces-muted">Rename: '+escapeHtml(renameMode)+'</div>' : '') +
      (createMode ? '<div class="capy-spaces-muted">Create folder: '+escapeHtml(createMode)+'</div>' : '') +
      '</div>' : '';
    const attachmentRows = attachmentItems.slice(0, 8).map(function(item){
      const name = safeWeatherText(item && item.name, 120);
      const kind = safeWeatherText(item && item.kind, 40);
      const itemStatus = safeWeatherText(item && item.status, 40);
      if (!name) return '';
      return '<div class="capy-spaces-muted">• '+escapeHtml(name)+(kind ? ' · '+escapeHtml(kind) : '')+(itemStatus ? ' · '+escapeHtml(itemStatus) : '')+'</div>';
    }).filter(Boolean);
    const attachmentCount = Number(flow && flow.attachment_count) || attachmentRows.length;
    const attachmentStorage = safeWeatherText(attachmentsInfo.storage || attachmentsInfo.status, 80);
    const attachmentPreview = attachmentRows.length ? '<div class="capy-spaces-card"><strong>Attachment preview</strong>' +
      '<div class="capy-spaces-muted">Attachments: '+escapeHtml(attachmentCount)+'</div>' +
      (attachmentStorage ? '<div class="capy-spaces-muted">Storage: '+escapeHtml(attachmentStorage)+'</div>' : '') +
      '<div>'+attachmentRows.join('')+'</div></div>' : '';
    if (status) rows.push('<div class="capy-spaces-muted">Draft status: '+escapeHtml(status)+'</div>');
    if (format) rows.push('<div class="capy-spaces-muted">Format: '+escapeHtml(format)+'</div>');
    if (editorBody) rows.push('<div>'+escapeHtml(editorBody)+'</div>');
    if (previewBody) rows.push('<div class="capy-spaces-muted">Preview: '+escapeHtml(previewBody)+'</div>');
    if (!rows.length && !folderPreview && !attachmentPreview) return '';
    return '<div class="capy-spaces-card capy-spaces-notes-smoke"><h4>Saved notes preview</h4>' +
      renderNotesFlowChecklist(flow) +
      folderPreview +
      attachmentPreview +
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

  function renderSnakeRepairPreview(flow){
    const safeFlow = flow && typeof flow === 'object' && !Array.isArray(flow) ? flow : {};
    const game = safeWeatherText(safeFlow.game, 80);
    const firstAttempt = safeWeatherText(safeFlow.first_attempt, 120);
    const bugReport = safeWeatherText(safeFlow.bug_report, 240);
    const repairEvent = safeWeatherText(safeFlow.repair_event, 120);
    const rendererStatus = safeWeatherText(safeFlow.render_status || safeFlow.renderer_status, 120);
    const focusPolicy = safeWeatherText(safeFlow.focus_policy, 120);
    const rollback = safeWeatherText(safeFlow.rollback, 120);
    if (!game && !firstAttempt && !bugReport && !repairEvent && !rendererStatus && !focusPolicy && !rollback) return '';
    return '<div class="capy-spaces-card capy-spaces-snake-smoke"><h4>Snake repair preview</h4>' +
      '<div class="capy-spaces-muted">Visible metadata-only game repair loop. Generated canvas code remains disabled until sandbox approval.</div>' +
      (game ? '<div class="capy-spaces-muted">Game: '+escapeHtml(game)+'</div>' : '') +
      (firstAttempt ? '<div class="capy-spaces-muted">First attempt: '+escapeHtml(firstAttempt)+'</div>' : '') +
      (bugReport ? '<div class="capy-spaces-muted">Bug report: '+escapeHtml(bugReport)+'</div>' : '') +
      (repairEvent ? '<div class="capy-spaces-muted">Repair event: '+escapeHtml(repairEvent)+'</div>' : '') +
      (rendererStatus ? '<div class="capy-spaces-muted">Renderer status: '+escapeHtml(rendererStatus)+'</div>' : '') +
      (focusPolicy ? '<div class="capy-spaces-muted">Focus policy: '+escapeHtml(focusPolicy)+'</div>' : '') +
      (rollback ? '<div class="capy-spaces-muted">Rollback: '+escapeHtml(rollback)+'</div>' : '') +
      '</div>';
  }

  function renderStockSmokePreview(stockSnapshot){
    const snapshot = stockSnapshot && typeof stockSnapshot === 'object' && !Array.isArray(stockSnapshot) ? stockSnapshot : {};
    const rows = Array.isArray(snapshot.rows) ? snapshot.rows : [];
    const networkMode = safeWeatherText(snapshot.network_mode || snapshot.network, 80);
    const status = safeWeatherText(snapshot.status, 80);
    const rowHtml = rows.slice(0, 8).map(function(row){
      const symbol = safeWeatherText(row && row.symbol, 20);
      const last = safeWeatherText(row && row.last, 40);
      const change = safeWeatherText(row && row.change, 40);
      const notes = safeWeatherText(row && row.notes, 120);
      const parts = [symbol, last, change, notes].filter(Boolean);
      return parts.length ? '<div class="capy-spaces-muted">'+parts.map(escapeHtml).join(' · ')+'</div>' : '';
    }).filter(Boolean).join('');
    if (!rowHtml && !networkMode && !status) return '';
    return '<div class="capy-spaces-card capy-spaces-stock-smoke"><h4>Stock chart preview</h4>' +
      '<div class="capy-spaces-muted">Visible metadata-only market snapshot. Live market data remains agent-mediated.</div>' +
      (status ? '<div class="capy-spaces-muted">Status: '+escapeHtml(status)+'</div>' : '') +
      '<div class="capy-spaces-widget-list"><div class="capy-spaces-widget"><div>'+rowHtml+'</div></div></div>' +
      (networkMode ? '<div class="capy-spaces-muted">Network mode: '+escapeHtml(networkMode)+'</div>' : '') +
      '</div>';
  }

  function renderMusicSmokePreview(flow){
    const safeFlow = flow && typeof flow === 'object' && !Array.isArray(flow) ? flow : {};
    const steps = Number(safeFlow.pattern_steps || 0);
    const webaudioPermission = safeWeatherText(safeFlow.webaudio_permission, 80);
    const cleanup = safeWeatherText(safeFlow.cleanup, 80);
    const sequencerReady = safeFlow.sequencer_ready ? 'ready' : '';
    const pianoRollState = safeFlow.piano_roll_ready ? 'metadata-only' : '';
    if (!steps && !webaudioPermission && !cleanup && !sequencerReady && !pianoRollState) return '';
    return '<div class="capy-spaces-card capy-spaces-music-smoke"><h4>Music sequencer preview</h4>' +
      '<div class="capy-spaces-muted">Visible metadata-only step sequencer and piano roll state. WebAudio and generated playback stay disabled until explicit approval.</div>' +
      (steps ? '<div class="capy-spaces-muted">Pattern: '+escapeHtml(steps)+' steps saved</div>' : '') +
      (webaudioPermission ? '<div class="capy-spaces-muted">WebAudio: disabled until approved</div>' : '') +
      (pianoRollState ? '<div class="capy-spaces-muted">Piano roll: '+escapeHtml(pianoRollState)+'</div>' : '') +
      (cleanup ? '<div class="capy-spaces-muted">Cleanup: '+escapeHtml(cleanup)+'</div>' : '') +
      '</div>';
  }

  function renderWidgetDetailPanel(spaceId, widget, runtimeContract){
    const safeWidget = widget && typeof widget === 'object' ? widget : {};
    const widgetId = safeWidget.id || '';
    const title = safeDisplayMetadataText(safeWidget.title, widgetId || 'Untitled widget') || widgetId || 'Untitled widget';
    const kind = safeDisplayMetadataText(safeWidget.kind, 'custom') || 'custom';
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
      renderSandboxRuntimeShell(spaceId || '', widgetId, title) +
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
    if (action === 'previewCreatorSpec') {
      const root = document.getElementById('capySpacesRoot');
      const promptInput = getRootInput(root, '#capyCreatorPrompt');
      const targetInput = getRootInput(root, '#capyCreatorTargetSpaceId');
      const prompt = promptInput && promptInput.value ? String(promptInput.value) : '';
      const targetSpaceId = safeCreatorIdText(targetInput && targetInput.value ? String(targetInput.value) : '');
      const payload = {action: 'space.creator.preview', prompt: prompt};
      if (targetSpaceId) payload.space_id = targetSpaceId;
      const data = await postSpacesJson('api/spaces/tool', payload);
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) refreshedRoot.innerHTML = renderCreatorPreviewResult(data || {}) + refreshedRoot.innerHTML;
      return;
    }
    if (action === 'commitCreatorSpec') {
      const previewId = button.dataset.previewId || '';
      if (!previewId) return;
      if (typeof showConfirmDialog !== 'function') return;
      const confirmed = await showConfirmDialog({
        title: 'Commit creator preview?',
        message: 'Commit this sandbox-previewed, visually QA-approved creator spec as a revisioned metadata-only Space.',
        confirmLabel: 'Commit revision',
        cancelLabel: 'Cancel',
        danger: true,
      });
      if (!confirmed) return;
      let data;
      try {
        data = await postSpacesJson('api/spaces/tool', {
          action: 'space.creator.commit',
          preview_id: previewId,
          sandbox_previewed: true,
          visual_qa_passed: true,
          approve_commit: true,
        });
      } catch (commitErr) {
        const refreshedRoot = document.getElementById('capySpacesRoot');
        if (refreshedRoot) refreshedRoot.innerHTML = renderCreatorCommitBlockedResult() + refreshedRoot.innerHTML;
        return;
      }
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) refreshedRoot.innerHTML = renderCreatorCommitResult(data || {}) + refreshedRoot.innerHTML;
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
    if (action === 'runWeatherWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_weather_widget'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runNotesWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_notes_app'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runKanbanWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_kanban_board'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runSnakeWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_snake_iterative_repair'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runDashboardWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_daily_dashboard'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runCameraWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_camera_dashboard'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runStockWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_stock_chart'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runMusicWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_step_sequencer_piano_roll'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runProviderSetupWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_provider_setup'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runBigBangWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_big_bang_onboarding'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runLocalServiceWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_local_agent_control_dashboard'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runTimeTravelWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_time_travel_restore'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runAdminRecoveryWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_safe_admin_recovery'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runBrowserWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_browser_cocontrol_google_or_test_site'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
      return;
    }
    if (action === 'runResearchWalkthrough') {
      const data = await postSpacesJson('api/spaces/demo/run', {demo: 'demo_research_harness_pdf_export'});
      await loadCapySpaces();
      const refreshedRoot = document.getElementById('capySpacesRoot');
      if (refreshedRoot) {
        const resultHtml = renderDemoSmokeResult(data || {});
        const space = data && data.space && typeof data.space === 'object' ? data.space : {};
        const demoSpaceId = space.space_id ? String(space.space_id) : '';
        if (demoSpaceId) {
          try {
            const eventsData = await fetchSpacesJson('api/spaces/widget/events?space_id='+encodeURIComponent(demoSpaceId));
            const widgetsData = await fetchSpacesJson('api/spaces/widgets?space_id='+encodeURIComponent(demoSpaceId));
            refreshedRoot.dataset.editingWidgetId = '';
            refreshedRoot.innerHTML = resultHtml + renderWidgetManager(demoSpaceId, widgetsData.widgets || [], eventsData.events || []);
          } catch (widgetErr) {
            refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
          }
        } else {
          refreshedRoot.innerHTML = resultHtml + refreshedRoot.innerHTML;
        }
      }
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
      const result = await postSpacesJson('api/spaces/templates/install', {template: 'model-setup'});
      await loadCapySpaces();
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderTemplateInstallStatus(result || {}) + root.innerHTML;
      return;
    }
    if (action === 'installGameTemplate') {
      const result = await postSpacesJson('api/spaces/templates/install', {template: 'game'});
      await loadCapySpaces();
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderTemplateInstallStatus(result || {}) + root.innerHTML;
      return;
    }
    if (action === 'installMusicTemplate') {
      const result = await postSpacesJson('api/spaces/templates/install', {template: 'music'});
      await loadCapySpaces();
      const root = document.getElementById('capySpacesRoot');
      if (root) root.innerHTML = renderTemplateInstallStatus(result || {}) + root.innerHTML;
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
    if (action === 'restoreWidgetRevision') {
      const eventId = button.dataset.eventId || '';
      const widgetId = button.dataset.widgetId || '';
      if (!spaceId || !eventId || !widgetId || typeof showConfirmDialog !== 'function') return;
      const ok = await showConfirmDialog({title: 'Restore widget revision?', message: 'Restore widget "'+widgetId+'" from revision '+eventId.slice(0, 12)+'? Other widgets in this Space are left unchanged.', confirmLabel: 'Restore widget', danger: true, focusCancel: true});
      if (!ok) return;
      await postSpacesJson('api/spaces/revision/restore-widget', {space_id: spaceId, event_id: eventId, widget_id: widgetId});
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
    ensureCapyWidgetRuntimeMessageHandler();
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
      const previewText = formatRestorePreview(rev && rev.restore_preview);
      const diffText = formatRestoreDiff(rev && rev.restore_diff);
      const restoreButton = eventId ? '<button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="restoreRecoveryRevision" data-space-id="'+escapeHtml(spaceId)+'" data-event-id="'+escapeHtml(eventId)+'">Restore revision</button>' : '';
      const widgetRestoreButtons = renderRestoreWidgetButtons(spaceId, eventId, rev && rev.restore_diff, 'restoreRecoveryWidgetRevision');
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(eventType)+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml(formatRevisionTime(rev && rev.created_at))+' · '+escapeHtml(eventId.slice(0, 12) || 'no-event-id')+'</div>' +
        (detailText ? '<div class="capy-spaces-muted">'+escapeHtml(detailText)+'</div>' : '') +
        (previewText ? '<div class="capy-spaces-muted">'+escapeHtml(previewText)+'</div>' : '') +
        (diffText ? '<div class="capy-spaces-muted">'+escapeHtml(diffText)+'</div>' : '') +
        '</div><div class="capy-spaces-actions">'+restoreButton+widgetRestoreButtons+'</div></div>';
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

  function renderRecoverySpaceEventStatus(space){
    const count = Number(space && space.queued_space_repair_count || 0);
    if (!count) return '';
    const latest = space && space.latest_space_repair_event && typeof space.latest_space_repair_event === 'object' ? space.latest_space_repair_event : {};
    const parts = [];
    const eventName = latest.event_name ? String(latest.event_name) : '';
    const status = latest.status ? String(latest.status) : '';
    const state = [eventName, status].filter(Boolean).join(' · ');
    parts.push('Space repair queued:'+(state ? ' '+state : ' '+count));
    if (latest.event_id) parts.push('Event: '+String(latest.event_id).slice(0, 12));
    return '<div class="capy-spaces-muted">'+escapeHtml(parts.join(' · '))+'</div>';
  }

  function recoveryCountLabel(count, singular){
    return Number(count || 0)+' '+singular+(Number(count || 0) === 1 ? '' : 's');
  }

  function renderRecoveryAdminGate(data){
    const gate = data && data.safe_admin && typeof data.safe_admin === 'object' && !Array.isArray(data.safe_admin) ? data.safe_admin : {};
    const summary = data && data.summary && typeof data.summary === 'object' && !Array.isArray(data.summary) ? data.summary : {};
    const labels = Array.isArray(gate.gate_labels) ? gate.gate_labels.slice(0, 6).map(safeCreatorSummaryText).filter(Boolean) : [];
    const route = safeCreatorSummaryText(gate.recovery_route || '');
    const restoreRoutes = Array.isArray(gate.restore_routes) ? gate.restore_routes.slice(0, 4).map(safeCreatorSummaryText).filter(Boolean) : [];
    const summaryText = [
      recoveryCountLabel(summary.space_count, 'space'),
      recoveryCountLabel(summary.widget_count, 'widget'),
      recoveryCountLabel(summary.disabled_space_count, 'disabled space'),
      recoveryCountLabel(summary.disabled_widget_count, 'disabled widget'),
      recoveryCountLabel(summary.rollback_point_count, 'rollback point'),
      recoveryCountLabel(summary.queued_event_count, 'queued event'),
      recoveryCountLabel(summary.module_count, 'module'),
      recoveryCountLabel(summary.disabled_module_count, 'disabled module'),
    ].join(' · ');
    const gateText = labels.length ? labels.join(' · ') : 'metadata-only recovery · generated widgets not rendered';
    return '<div class="capy-spaces-card"><h4>Recovery hard gate</h4>' +
      '<div class="capy-spaces-muted">'+escapeHtml(gateText)+'</div>' +
      '<div class="capy-spaces-muted">Recovery summary: '+escapeHtml(summaryText)+'</div>' +
      (route || restoreRoutes.length ? '<div class="capy-spaces-muted">Routes: '+escapeHtml([route].concat(restoreRoutes).filter(Boolean).join(' · '))+'</div>' : '') +
      '</div>';
  }

  function renderRecoveryModules(data){
    const modules = Array.isArray(data && data.modules) ? data.modules.slice(0, 20) : [];
    if (!modules.length) return '';
    const rows = modules.map(function(module){
      const rawModuleId = module && (module.module_id || module.id) ? String(module.module_id || module.id) : '';
      const moduleId = safeCreatorSummaryText(rawModuleId);
      const name = safeCreatorSummaryText(module && module.name || moduleId || 'Untitled module');
      const description = safeCreatorSummaryText(module && module.description || '');
      const scope = safeCreatorSummaryText(module && module.scope || 'global');
      const disabled = !!(module && module.disabled);
      const disabledReason = safeCreatorSummaryText(module && module.disabled_reason || '');
      const revision = safeCreatorSummaryText(module && module.revision_event_id || '');
      const actionModuleId = safeCreatorIdText(rawModuleId);
      const moduleAction = actionModuleId ? (disabled
        ? '<button type="button" class="capy-spaces-btn" data-capy-action="enableRecoveryModule" data-module-id="'+escapeHtml(actionModuleId)+'">Enable module</button>'
        : '<button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="disableRecoveryModule" data-module-id="'+escapeHtml(actionModuleId)+'">Disable module</button>') : '';
      return '<div class="capy-spaces-widget"><div><strong>'+escapeHtml(name || moduleId || 'Untitled module')+'</strong>' +
        '<div class="capy-spaces-muted">'+escapeHtml([scope, moduleId].filter(Boolean).join(' · '))+'</div>' +
        (description ? '<div class="capy-spaces-muted">'+escapeHtml(description)+'</div>' : '') +
        (disabled ? '<div class="capy-spaces-muted">Disabled'+(disabledReason ? ': '+escapeHtml(disabledReason) : '')+'</div>' : '') +
        (revision ? '<div class="capy-spaces-muted">Revision: '+escapeHtml(revision.slice(0, 12))+'</div>' : '') +
        '</div><div class="capy-spaces-actions"><span class="capy-spaces-muted">metadata-only</span>'+moduleAction+'</div></div>';
    }).join('');
    return '<div class="capy-spaces-card"><h4>Quarantined modules</h4>' +
      '<div class="capy-spaces-muted">Generated module bodies stay quarantined for repair/rollback; this panel shows safe metadata only.</div>' +
      '<div class="capy-spaces-widget-list">'+rows+'</div></div>';
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
      const spaceDisabledReason = s.disabled_reason ? safeDisplayMetadataText(String(s.disabled_reason), '[REDACTED]') : '';
      const spaceStatus = spaceDisabled ? '<div class="capy-spaces-muted">Space disabled'+(spaceDisabledReason ? ': '+escapeHtml(spaceDisabledReason) : '')+'</div>' : '';
      const spaceAction = spaceDisabled
        ? '<button type="button" class="capy-spaces-btn" data-capy-action="enableRecoverySpace" data-space-id="'+escapeHtml(spaceId)+'">Enable space</button>'
        : '<button type="button" class="capy-spaces-btn capy-spaces-danger" data-capy-action="disableRecoverySpace" data-space-id="'+escapeHtml(spaceId)+'">Disable space</button>';
      const spaceRepairAction = '<button type="button" class="capy-spaces-btn" data-capy-action="repairRecoverySpace" data-space-id="'+escapeHtml(spaceId)+'">Ask Capy to repair Space</button>';
      const spaceExportActions = '<button type="button" class="capy-spaces-btn" data-capy-action="exportRecoverySpaceYaml" data-space-id="'+escapeHtml(spaceId)+'">Export YAML</button>' +
        '<button type="button" class="capy-spaces-btn" data-capy-action="exportRecoverySpaceZip" data-space-id="'+escapeHtml(spaceId)+'">Export ZIP</button>';
      const widgetRows = widgets.length ? '<div class="capy-spaces-widget-list">'+widgets.map(function(w){
        const widgetId = w && w.id ? String(w.id) : '';
        const title = w && w.title ? String(w.title) : widgetId || 'Untitled widget';
        const kind = w && w.kind ? String(w.kind) : 'custom';
        const disabled = !!(w && w.disabled);
        const disabledReason = w && w.disabled_reason ? safeDisplayMetadataText(String(w.disabled_reason), '[REDACTED]') : '';
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
        renderRecoverySpaceEventStatus(s || {}) +
        '<div class="capy-spaces-muted">Space ID: '+escapeHtml(spaceId)+' · Widgets: '+Number(s.widget_count||0)+' · Revision: '+escapeHtml(s.revision_event_id||'none')+'</div>' +
        '<div class="capy-spaces-actions">'+spaceAction+spaceRepairAction+spaceExportActions+'</div>' +
        widgetRows +
        '<div class="capy-spaces-card"><h4>Recovery rollback</h4><div class="capy-spaces-muted">Restore safe metadata snapshots without rendering generated widget bodies.</div><div class="capy-spaces-widget-list">'+recoveryRows+'</div></div>' +
        '</div></div>';
    }).join('') : '<div class="capy-spaces-muted">No spaces found in recovery metadata.</div>';
    return '<div class="capy-spaces-card"><h3>Safe recovery</h3>' +
      '<div class="capy-spaces-muted">Generated widgets rendered: '+String(!!data.generated_widgets_rendered)+'. This panel lists metadata only so broken generated UI cannot execute here.</div>' +
      renderRecoveryAdminGate(data || {}) +
      renderRecoveryModules(data || {}) +
      '<div class="capy-spaces-widget-list">'+rows+'</div></div>';
  }

  async function handleCapySpacesRecoveryClick(event){
    const button = event.target && event.target.closest ? event.target.closest('[data-capy-action]') : null;
    if (!button) return;
    const action = button.dataset.capyAction;
    if (action !== 'disableRecoveryWidget' && action !== 'enableRecoveryWidget' && action !== 'disableRecoverySpace' && action !== 'enableRecoverySpace' && action !== 'repairRecoverySpace' && action !== 'exportRecoverySpaceYaml' && action !== 'exportRecoverySpaceZip' && action !== 'disableRecoveryModule' && action !== 'enableRecoveryModule' && action !== 'repairRecoveryWidget' && action !== 'restoreRecoveryRevision' && action !== 'restoreRecoveryWidgetRevision') return;
    if (action === 'disableRecoveryModule' || action === 'enableRecoveryModule') {
      if (typeof showConfirmDialog !== 'function') return;
      const moduleId = button.dataset.moduleId || '';
      if (!moduleId) return;
      if (action === 'disableRecoveryModule') {
        const ok = await showConfirmDialog({title: 'Disable module?', message: 'Disable quarantined module "'+moduleId+'" from safe recovery? The raw body is preserved only for repair/rollback.', confirmLabel: 'Disable module', danger: true, focusCancel: true});
        if (!ok) return;
        await postSpacesJson('api/spaces/recovery/disable-module', {module_id: moduleId, reason: 'disabled from recovery panel'});
        await loadCapySpacesRecovery();
        return;
      }
      const ok = await showConfirmDialog({title: 'Enable module?', message: 'Re-enable quarantined module "'+moduleId+'" in safe recovery? Generated module bodies are still not rendered.', confirmLabel: 'Enable module', danger: true, focusCancel: true});
      if (!ok) return;
      await postSpacesJson('api/spaces/recovery/enable-module', {module_id: moduleId, reason: 'enabled from recovery panel'});
      await loadCapySpacesRecovery();
      return;
    }
    const spaceId = button.dataset.spaceId || '';
    if (!spaceId) return;
    if (action === 'exportRecoverySpaceYaml' || action === 'exportRecoverySpaceZip') {
      const format = action === 'exportRecoverySpaceZip' ? 'zip' : 'yaml';
      const data = await postSpacesJson('api/spaces/export', {space_id: spaceId, format: format});
      const recoveryRoot = document.getElementById('capySpacesRecovery');
      if (recoveryRoot) recoveryRoot.innerHTML = renderSpaceExportResult(spaceId, data || {}) + recoveryRoot.innerHTML;
      return;
    }
    if (action === 'repairRecoverySpace') {
      if (typeof showPromptDialog !== 'function') return;
      const promptText = await showPromptDialog({
        title: 'Ask Capy to repair Space',
        placeholder: 'Describe what is broken in Space '+spaceId,
        confirmLabel: 'Queue repair',
      });
      if (!promptText) return;
      await postSpacesJson('api/spaces/recovery/repair-space', {
        space_id: spaceId,
        prompt: promptText,
        payload: {source: 'recovery-panel', action: 'repair-space'},
      });
      await loadCapySpacesRecovery();
      return;
    }
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
    if (action === 'restoreRecoveryWidgetRevision') {
      const eventId = button.dataset.eventId || '';
      const widgetId = button.dataset.widgetId || '';
      if (!eventId || !widgetId) return;
      const ok = await showConfirmDialog({title: 'Restore recovery widget revision?', message: 'Restore widget "'+widgetId+'" from revision '+eventId.slice(0, 12)+' in safe recovery? Other widgets are left unchanged, and generated widget bodies are not displayed here.', confirmLabel: 'Restore widget', danger: true, focusCancel: true});
      if (!ok) return;
      await postSpacesJson('api/spaces/revision/restore-widget', {space_id: spaceId, event_id: eventId, widget_id: widgetId});
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
