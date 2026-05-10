"""Behavioural tests for the actual Capy Spaces browser shell in static/spaces.js."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SPACES_JS_PATH = REPO_ROOT / "static" / "spaces.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

const calls = [];
const dialogs = [];
const switchedPanels = [];
const capySpaceSyncs = [];
const windowListeners = {};
let beforeHtml = '';
const values = {
  '#capyWidgetId': 'notes',
  '#capyWidgetTitle': 'Notes',
  '#capyWidgetKind': 'markdown',
  '#capyWidgetX': '2',
  '#capyWidgetY': '3',
  '#capyWidgetW': '8',
  '#capyWidgetH': '5',
  '#capySpaceId': 'ops',
  '#capySpaceName': 'Ops',
  '#capySpaceDescription': '<b>Operations</b>',
  '#capySpaceAgentImportSpaceYaml': 'id: imported-lab\nname: Imported Lab\ndescription: Imported safely\n',
  '#capySpaceAgentImportWidgetsJson': JSON.stringify({'widgets/weather.yaml': 'id: weather\ntitle: Weather\ntype: html\nrenderer: <script>bad()</script>\napi_key: SECRET'}, null, 2),
  '#capySpaceAgentImportZipB64': 'UEsDBBQAAAAIAAxTSFsAAAAAAAAAAAAAAAALAAAAc3BhY2UueWFtbA==',
  '#capyWidgetNotesBody': 'Initial notes body',
  '#capyCreatorPrompt': 'Create an ops dashboard without leaking SECRET_VALUE_DO_NOT_LEAK or <script>bad()</script>',
  '#capyCreatorTargetSpaceId': '',
};
const inputs = {};
const elements = {};
function makeInput(selector) {
  if (!(selector in values)) return null;
  return inputs[selector] || (inputs[selector] = {
    get value() { return values[selector]; },
    set value(next) { values[selector] = next; },
  });
}
function makeElement(id) {
  return elements[id] || (elements[id] = {
    id,
    dataset: {},
    innerHTML: '',
    textContent: '',
    listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    querySelector(selector) {
      return makeInput(selector);
    },
  });
}
function response(data) {
  return { ok: true, status: 200, json: async () => data };
}

global.window = {
  addEventListener(type, fn) {
    if (type === 'DOMContentLoaded') this._domReady = fn;
    windowListeners[type] = fn;
  },
};
global.S = { session: { session_id: 'session-123', active_space_id: null } };
global.switchPanel = async function(panel) { switchedPanels.push(panel); return true; };
global.syncCapyActiveSpaceContext = function() { capySpaceSyncs.push(global.S && global.S.session ? global.S.session.active_space_id : null); };
global.document = {
  getElementById: makeElement,
};
global.fetch = async function(path, opts = {}) {
  calls.push({ path, method: opts.method || 'GET', body: opts.body || '' });
  if (path === 'api/spaces') {
    if (String(scenario || '').startsWith('resetBigBang')) {
      return response({ enabled: true, spaces: [{ space_id: 'big-bang-onboarding', name: 'Big Bang Onboarding', widget_count: 4, revision_event_id: 'rev-reset-bigbang' }] });
    }
    return response({ enabled: true, spaces: [{ space_id: 'lab', name: 'Lab', widget_count: 1, revision_event_id: 'rev1' }] });
  }
  if (path === 'api/spaces/demo/runs') {
    return response({
      ok: true,
      demos: [
        { demo: 'demo_weather_widget', template: 'weather', title: 'Weather answer → persistent widget', mode: 'metadata-only-smoke', renderer: '<script>bad()</script>', api_key: 'SECRET' },
        { demo: 'demo_notes_app', template: 'notes', title: 'Notes app', mode: 'metadata-only-smoke', renderer: '<script>bad()</script>', api_key: 'SECRET' },
        { demo: 'demo_kanban_board', template: 'kanban', title: 'Kanban board', mode: 'metadata-only-smoke', renderer: '<script>bad()</script>', api_key: 'SECRET' },
        { demo: 'demo_snake_iterative_repair', template: 'game', title: 'Snake repair loop', mode: 'metadata-only-smoke', renderer: '<script>bad()</script>', api_key: 'SECRET' },
        { demo: 'demo_research_harness_pdf_export', template: 'research', title: 'Research harness PDF export', mode: 'metadata-only-smoke', source: 'SECRET_SOURCE' },
        { demo: 'demo_time_travel_restore', template: 'big-bang', title: 'Time travel rollback', mode: 'metadata-only-smoke', source: 'SECRET_SOURCE' },
        { demo: 'demo_big_bang_onboarding', template: 'big-bang', title: 'Big Bang onboarding', mode: 'metadata-only-smoke', renderer: '<script>bad()</script>', api_key: 'SECRET' },
        { demo: 'demo_safe_admin_recovery', template: 'weather', title: 'Admin recovery', mode: 'metadata-only-smoke', source: 'SECRET_SOURCE' },
      ],
    });
  }
  if (path === 'api/spaces/demo/run') {
    const body = opts.body ? JSON.parse(opts.body) : {};
    const demo = body.demo || 'demo_weather_widget';
    const isResearch = demo === 'demo_research_harness_pdf_export';
    const isNotes = demo === 'demo_notes_app';
    const isKanban = demo === 'demo_kanban_board';
    const isDashboard = demo === 'demo_daily_dashboard';
    const isSnake = demo === 'demo_snake_iterative_repair';
    const isStock = demo === 'demo_stock_chart';
    const isCamera = demo === 'demo_camera_dashboard';
    const isService = demo === 'demo_local_agent_control_dashboard';
    const isMusic = demo === 'demo_step_sequencer_piano_roll';
    const isProviderSetup = demo === 'demo_provider_setup';
    const isBigBang = demo === 'demo_big_bang_onboarding';
    const isTimeTravel = demo === 'demo_time_travel_restore';
    const isRecovery = demo === 'demo_safe_admin_recovery';
    const kanbanColumns = [
      { id: 'kanban-backlog', kind: 'kanban-column', title: 'Backlog', metadata: { kanban: { status: 'board-ready', column: 'Backlog', color: 'blue', cards: [{ id: 'card-plan', title: 'Plan the first task', status: 'todo' }], interaction: { drag_drop: 'planned', edit_cards: 'metadata-only' }, renderer: '<script>bad()</script>', api_key: 'SECRET' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'kanban-doing', kind: 'kanban-column', title: 'Doing', metadata: { kanban: { status: 'board-ready', column: 'Doing', color: 'amber', cards: [{ id: 'card-build', title: 'Build metadata-only board preview', status: 'doing' }], interaction: { drag_drop: 'planned', edit_cards: 'metadata-only' } } } },
      { id: 'kanban-done', kind: 'kanban-column', title: 'Done', metadata: { kanban: { status: 'board-ready', column: 'Done', color: 'green', cards: [{ id: 'card-install', title: 'Install board template', status: 'done' }], interaction: { drag_drop: 'planned', edit_cards: 'metadata-only' } } } },
    ];
    const dashboardWidgets = [
      { id: 'dashboard-prices', kind: 'chart', title: 'Market prices', metadata: { market: { status: 'ready', symbols: ['NVDA', 'AAPL', 'GOOGL'], network: 'agent-mediated', renderer: '<script>bad()</script>', api_key: 'SECRET' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'dashboard-news', kind: 'news', title: 'News brief', metadata: { news: { status: 'ready', source: 'agent-mediated', token: 'SECRET_VALUE_DO_NOT_LEAK' } } },
      { id: 'dashboard-agenda', kind: 'agenda', title: 'Today agenda', metadata: { agenda: { status: 'ready', items: ['Morning brief', 'Market check'] } } },
      { id: 'dashboard-brief', kind: 'markdown', title: 'Daily brief', metadata: { notes: { status: 'ready', summary: 'Daily dashboard metadata persisted.' } } },
    ];
    const cameraWidgets = [
      { id: 'camera-grid', kind: 'camera-grid', title: 'Camera grid', metadata: { cameras: { status: 'approval-required', network: 'agent-mediated', streams: [], renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'camera-permissions', kind: 'status', title: 'Stream permissions', metadata: { permissions: { camera_urls: 'approval-required', network: 'agent-mediated', authorization: 'bearer placeholder' } } },
      { id: 'camera-incidents', kind: 'table', title: 'Incident notes', metadata: { incidents: { status: 'empty', rows: [], source: 'SECRET_SOURCE' } }, source: 'SECRET_SOURCE' },
    ];
    const stockRows = [
      { symbol: 'NVDA', last: '905.10', change: '+1.8%', notes: 'GPU demand watch', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
      { symbol: 'AAPL', last: '182.40', change: '-0.3%', notes: 'services margin watch' },
      { symbol: 'GOOGL', last: '171.25', change: '+0.6%', notes: 'AI search watch', renderer: '<script>bad()</script>' },
    ];
    const stockWidgets = [
      { id: 'stock-chart', kind: 'chart', title: 'NVDA / AAPL / GOOGL', metadata: { market_data: { status: 'market-snapshot-ready', series: ['NVDA', 'AAPL', 'GOOGL'], network: 'agent-mediated', rows: stockRows, renderer: '<script>bad()</script>', api_key: 'SECRET' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'stock-watchlist', kind: 'table', title: 'Watchlist', metadata: { watchlist: { status: 'market-snapshot-ready', rows: stockRows, authorization: 'bearer placeholder' } } },
      { id: 'stock-notes', kind: 'markdown', title: 'Market notes', metadata: { notes: { status: 'ready', summary: 'Demo market snapshot is agent-mediated.' } } },
    ];
    const serviceWidgets = [
      { id: 'service-api-chat', kind: 'api-connector', title: 'Service API chat', metadata: { connector: { target: 'local-service', mode: 'agent-mediated', auth: 'configured-outside-widget', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'service-browser-panel', kind: 'browser-surface', title: 'Service browser panel', metadata: { browser_surface: { url: 'about:blank', inspection: 'metadata-only', approval: 'required', authorization: 'bearer placeholder' } }, source: 'SECRET_SOURCE' },
      { id: 'service-health', kind: 'status', title: 'Health checks', metadata: { checks: { status: 'pending', endpoints: ['/health', 'api/status'], token: 'SECRET_VALUE_DO_NOT_LEAK' } } },
      { id: 'service-settings-review', kind: 'table', title: 'Settings review', metadata: { settings: { status: 'review-only', fields: ['provider', 'network', 'auth'], renderer: '<script>bad()</script>' } } },
    ];
    const musicWidgets = [
      { id: 'music-sequencer-grid', kind: 'step-sequencer', title: 'Step sequencer', metadata: { status: { pattern: 'demo-pattern-saved', steps: 16 }, audio_policy: { permission: 'explicit-user-gesture', webaudio: 'disabled-until-approved', cleanup: 'planned-on-rerender', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'music-synth-controls', kind: 'audio-controls', title: 'Synth controls', metadata: { audio_policy: { permission: 'explicit-user-gesture', webaudio: 'disabled-until-approved', token: 'SECRET_VALUE_DO_NOT_LEAK' } }, source: 'SECRET_SOURCE' },
      { id: 'music-piano-roll', kind: 'piano-roll', title: 'Piano roll', metadata: { interaction: { keyboard: 'explicit-focus', editing: 'metadata-only', renderer: '<script>bad()</script>' } } },
      { id: 'music-notes', kind: 'markdown', title: 'Music notes', metadata: { notes: { status: 'safe-metadata', summary: 'Piano-roll resize cleanup remains planned.' } } },
    ];
    const modelSetupWidgets = [
      { id: 'model-provider-status', kind: 'status', title: 'Provider status', metadata: { provider: { status: 'review-required', setup: 'external-cli-or-settings', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'model-local-runtime', kind: 'local-runtime', title: 'Local runtime', metadata: { runtime: { status: 'agent-mediated', lmstudio: 'optional', authorization: 'bearer placeholder' } }, source: 'SECRET_SOURCE' },
      { id: 'model-settings-review', kind: 'table', title: 'Settings review', metadata: { settings: { status: 'review-only', fields: ['provider', 'model', 'runtime'], token: 'SECRET_VALUE_DO_NOT_LEAK' } } },
      { id: 'model-next-steps', kind: 'checklist', title: 'Next steps', metadata: { checklist: { items: ['Choose provider', 'Validate model', 'Start first Space'], renderer: '<script>bad()</script>' } } },
    ];
    const bigBangWidgets = [
      { id: 'bigbang-welcome', kind: 'markdown', title: 'Welcome to Capy Spaces', metadata: { notes: { status: 'curated-metadata', summary: 'First-run tour for safe Spaces.', renderer: '<script>bad()</script>' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'bigbang-demo-launcher', kind: 'checklist', title: 'Demo launchers', metadata: { checklist: { items: ['weather', 'research', 'kanban', 'notes'], api_key: 'SECRET_VALUE_DO_NOT_LEAK' } } },
      { id: 'bigbang-safety', kind: 'status', title: 'Safety guardrails', metadata: { safety: { generated_code: 'disabled-by-default', recovery: 'available', authorization: 'bearer placeholder' } } },
      { id: 'bigbang-next-steps', kind: 'checklist', title: 'Next steps', metadata: { checklist: { items: ['Use this space in chat', 'Ask Capy to customize widgets'], renderer: '<script>bad()</script>' } } },
    ];
    if (demo === 'demo_browser_cocontrol_google_or_test_site') {
      return response({
        ok: true,
        action: 'browser-surface-seeded',
        demo: demo,
        template: 'browser',
        mode: 'metadata-only-smoke',
        space: { space_id: 'demo-browser-cocontrol-google-or-test-site', name: 'Browser Co-control Smoke', widget_count: 3, revision_event_id: 'rev-demo', renderer: '<script>bad()</script>', api_key: 'SECRET' },
        widgets: [
          { id: 'browser-panel', kind: 'browser-surface', title: 'Shared browser panel', renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { id: 'browser-controls', kind: 'browser-controls', title: 'Agent controls', renderer: '<script>bad()</script>' },
          { id: 'browser-notes', kind: 'markdown', title: 'Browser notes', source: 'SECRET_SOURCE' },
        ],
        widget_count: 3,
        persisted_widget_count: 3,
        persistence_checked: true,
        revision_event_count: 2,
        rollback_point: true,
      });
    }
    let demoAction = 'space.demo.run';
    let demoTemplate = 'weather';
    let demoSpaceId = 'demo-weather-widget';
    let demoSpaceName = 'Weather Demo Smoke';
    let demoWidgetCount = 1;
    let demoWidgets = [{ id: 'weather-current', kind: 'weather', title: 'Weather in Prague', renderer: '<script>bad()</script>', api_key: 'SECRET' }];
    if (isResearch) {
      demoAction = 'pdf-export-requested'; demoTemplate = 'research'; demoSpaceId = 'demo-research-harness-pdf-export'; demoSpaceName = 'Research Harness'; demoWidgetCount = 5;
      demoWidgets = [{ id: 'research-summary', kind: 'markdown', title: 'Summary report', renderer: '<script>bad()</script>', api_key: 'SECRET' }];
    } else if (isNotes) {
      demoAction = 'notes-draft-saved'; demoTemplate = 'notes'; demoSpaceId = 'demo-notes-app'; demoSpaceName = 'Notes App Smoke'; demoWidgetCount = 4;
      demoWidgets = [{ id: 'notes-editor', kind: 'rich-text-editor', title: 'Editor', renderer: '<script>bad()</script>', api_key: 'SECRET' }];
    } else if (isKanban) {
      demoAction = 'kanban-board-seeded'; demoTemplate = 'kanban'; demoSpaceId = 'demo-kanban-board'; demoSpaceName = 'Kanban Board Smoke'; demoWidgetCount = 4; demoWidgets = kanbanColumns;
    } else if (isDashboard) {
      demoAction = 'daily-dashboard-seeded'; demoTemplate = 'dashboard'; demoSpaceId = 'demo-daily-dashboard'; demoSpaceName = 'Daily Dashboard Smoke'; demoWidgetCount = 4; demoWidgets = dashboardWidgets;
    } else if (isSnake) {
      demoAction = 'snake-repair-queued'; demoTemplate = 'game'; demoSpaceId = 'demo-snake-iterative-repair'; demoSpaceName = 'Snake Repair Smoke'; demoWidgetCount = 3;
    } else if (isStock) {
      demoAction = 'stock-snapshot-recorded'; demoTemplate = 'stock'; demoSpaceId = 'demo-stock-chart'; demoSpaceName = 'Stock Chart Smoke'; demoWidgetCount = 3; demoWidgets = stockWidgets;
    } else if (isCamera) {
      demoAction = 'camera-dashboard-seeded'; demoTemplate = 'camera'; demoSpaceId = 'demo-camera-dashboard'; demoSpaceName = 'Camera Dashboard Smoke'; demoWidgetCount = 3; demoWidgets = cameraWidgets;
    } else if (isService) {
      demoAction = 'local-service-dashboard-seeded'; demoTemplate = 'service'; demoSpaceId = 'demo-local-agent-control-dashboard'; demoSpaceName = 'Local Service Dashboard Smoke'; demoWidgetCount = 4; demoWidgets = serviceWidgets;
    } else if (isMusic) {
      demoAction = 'music-pattern-seeded'; demoTemplate = 'music'; demoSpaceId = 'demo-step-sequencer-piano-roll'; demoSpaceName = 'Music Sequencer Smoke'; demoWidgetCount = 4; demoWidgets = musicWidgets;
    } else if (isProviderSetup) {
      demoAction = 'provider-setup-seeded'; demoTemplate = 'model-setup'; demoSpaceId = 'demo-provider-setup'; demoSpaceName = 'Provider Setup Smoke'; demoWidgetCount = 4; demoWidgets = modelSetupWidgets;
    } else if (isBigBang) {
      demoAction = 'big-bang-onboarding-seeded'; demoTemplate = 'big-bang'; demoSpaceId = 'demo-big-bang-onboarding'; demoSpaceName = 'Big Bang Onboarding Smoke'; demoWidgetCount = 4; demoWidgets = bigBangWidgets;
    } else if (isTimeTravel) {
      demoAction = 'restored'; demoTemplate = 'weather'; demoSpaceId = 'demo-time-travel-restore'; demoSpaceName = 'Time Travel Restore Smoke';
    } else if (isRecovery) {
      demoAction = 'recovery-disabled'; demoTemplate = 'weather'; demoSpaceId = 'demo-safe-admin-recovery'; demoSpaceName = 'Admin Recovery Smoke';
    }
    return response({
      ok: true,
      action: demoAction,
      demo: demo,
      template: demoTemplate,
      mode: 'metadata-only-smoke',
      space: {
        space_id: demoSpaceId,
        name: demoSpaceName,
        widget_count: demoWidgetCount,
        revision_event_id: 'rev-demo',
        renderer: '<script>bad()</script>',
        api_key: 'SECRET',
      },
      widgets: demoWidgets,
      weather_observation: demo === 'demo_weather_widget' ? { widget: { id: 'weather-current', kind: 'weather', title: 'Weather in Prague', metadata: { weather: { location: 'Prague', country: 'CZ', status: 'observation-ready', current: { condition: 'partly cloudy', temperature_c: '18', feels_like_c: '17' }, summary: 'Partly cloudy in Prague; refreshed through agent-mediated weather metadata.', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' } } : undefined,
      prompt_flow: demo === 'demo_weather_widget' ? { blank_space: true, query: 'What is the weather in Prague?', chat_answer_status: 'recorded', answer_preview: 'Prague is partly cloudy at 18 °C; the answer is now saved as safe widget metadata.', widget_request: 'show it to me in a widget', widget_created: true, reload_verified: true, network_mode: 'agent-mediated', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } : undefined,
      notes_flow: isNotes ? { folders_ready: true, folder_count: 2, active_folder: 'Demo Project', editor_saved: true, markdown_preview_saved: true, attachments_agent_mediated: true, renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } : undefined,
      notes_artifact: isNotes ? { folders: { id: 'notes-folders', kind: 'folder-list', title: 'Folders', metadata: { folders: [{ id: 'folder-inbox', title: 'Inbox', api_key: 'SECRET_VALUE_DO_NOT_LEAK' }, { id: 'folder-demo', title: 'Demo Project' }], interaction: { rename: 'metadata-only', create_folder: 'metadata-only', active_folder_id: 'folder-demo', renderer: '<script>bad()</script>' } }, renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' }, editor: { id: 'notes-editor', kind: 'rich-text-editor', title: 'Editor', metadata: { notes: { status: 'draft-saved', format: 'markdown', body: 'Demo note draft saved through typed Capy Spaces metadata.', renderer: '<script>bad()</script>', api_key: 'SECRET' } }, renderer: '<script>bad()</script>' }, preview: { id: 'notes-preview', kind: 'markdown', title: 'Markdown preview', metadata: { notes: { format: 'markdown', body: '# Demo note\n\nThis markdown preview was saved as metadata-only state.', source: 'SECRET_SOURCE' } } }, attachments: { id: 'notes-attachments', kind: 'attachment-list', title: 'Attachments', metadata: { attachments: { status: 'agent-mediated', storage: 'agent-mediated', items: [{ id: 'attachment-demo-markdown', name: 'demo-note.md', kind: 'markdown', status: 'ready', api_key: 'SECRET_VALUE_DO_NOT_LEAK' }, { id: 'attachment-whiteboard', name: 'whiteboard.png', kind: 'image', status: 'planned', renderer: '<script>bad()</script>' }] } }, renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } } : undefined,
      kanban_board: isKanban ? { status: 'board-ready', column_count: 3, columns: kanbanColumns, renderer: '<script>bad()</script>', api_key: 'SECRET' } : undefined,
      stock_snapshot: isStock ? { status: 'market-snapshot-ready', symbols: ['NVDA', 'AAPL', 'GOOGL'], network_mode: 'agent-mediated', rows: stockRows, renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } : undefined,
      music_flow: isMusic ? { sequencer_ready: true, pattern_steps: 16, piano_roll_ready: true, webaudio_permission: 'explicit-user-gesture', cleanup: 'planned-on-rerender', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } : undefined,
      snake_repair_flow: isSnake ? { game: 'snake', first_attempt: 'broken-placeholder', bug_report: 'Snake canvas needs explicit keyboard focus and collision repair before rendering is enabled.', repair_event: 'agent.repair', render_status: 'generated-code-disabled', focus_policy: 'explicit-click', rollback: 'revision-history', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } : undefined,
      widget_count: demoWidgetCount,
      persisted_widget_count: demoWidgetCount,
      persistence_checked: true,
      revision_event_count: 2,
      rollback_point: true,
      queued_event_count: isResearch ? 1 : (isSnake ? 1 : (isStock ? 1 : (isMusic ? 1 : 0))),
      research_rollback_check: isResearch ? { verified: true, restored_event_id: 'rev-before-export', restored_widget_count: 5, replayed_after_restore: true, renderer: '<script>bad()</script>', api_key: '***' } : undefined,
    });
  }
  if (path === 'api/spaces/demo/run-all') {
    return response({
      ok: true,
      action: 'space.demo.run_all',
      total: 5,
      passed: 5,
      failed: 0,
      mode: 'metadata-only-smoke',
      results: [
        { ok: true, demo: 'demo_weather_widget', template: 'weather', mode: 'metadata-only-smoke', space: { space_id: 'demo-weather-widget', name: 'Weather Demo Smoke', renderer: '<script>bad()</script>', api_key: 'UNTRUSTED_VALUE' }, widget_count: 1, persisted_widget_count: 1, rollback_point: true, persistence_checked: true, queued_event_count: 1, weather_observation: { widget: { id: 'weather-current', kind: 'weather', title: 'Weather in Prague', metadata: { weather: { location: 'Prague', country: 'CZ', status: 'observation-ready', current: { condition: 'partly cloudy', temperature_c: '18', feels_like_c: '17' }, summary: 'Partly cloudy in Prague; refreshed through agent-mediated weather metadata.', renderer: '<script>bad()</script>', api_key: 'UNTRUSTED_VALUE' } }, renderer: '<script>bad()</script>', api_key: 'UNTRUSTED_VALUE' } }, prompt_flow: { blank_space: true, chat_answer_status: 'recorded', widget_created: true, reload_verified: true, query: 'What is the weather in Prague?', answer_preview: 'Prague is partly cloudy at 18 °C; the answer is now saved as safe widget metadata.', widget_request: 'show it to me in a widget', network_mode: 'agent-mediated', renderer: '<script>bad()</script>', api_key: 'UNTRUSTED_VALUE' } },
        { ok: true, demo: 'demo_notes_app', template: 'notes', mode: 'metadata-only-smoke', space: { space_id: 'demo-notes-app', name: 'Notes App Smoke', source: 'UNTRUSTED_SOURCE' }, widget_count: 4, persisted_widget_count: 4, rollback_point: true, persistence_checked: true, queued_event_count: 1, notes_flow: { folders_ready: true, folder_count: 2, active_folder: 'Demo Project', editor_saved: true, markdown_preview_saved: true, attachments_agent_mediated: true, renderer: '<script>bad()</script>', api_key: 'UNTRUSTED_VALUE' } },
        { ok: true, demo: 'demo_kanban_board', template: 'kanban', mode: 'metadata-only-smoke', space: { space_id: 'demo-kanban-board', name: 'Kanban Board Smoke', source: 'UNTRUSTED_SOURCE' }, widget_count: 4, persisted_widget_count: 4, rollback_point: true, persistence_checked: true, kanban_board: { status: 'board-ready', column_count: 3, columns: [
          { id: 'kanban-backlog', kind: 'kanban-column', title: 'Backlog', metadata: { kanban: { status: 'board-ready', column: 'Backlog', color: 'blue', cards: [{ id: 'card-plan', title: 'Plan the first task', status: 'todo' }], interaction: { drag_drop: 'planned', edit_cards: 'metadata-only' }, renderer: '<script>bad()</script>', api_key: 'UNTRUSTED_VALUE' } }, renderer: '<script>bad()</script>', api_key: 'UNTRUSTED_VALUE' },
          { id: 'kanban-doing', kind: 'kanban-column', title: 'Doing', metadata: { kanban: { status: 'board-ready', column: 'Doing', color: 'amber', cards: [{ id: 'card-build', title: 'Build metadata-only board preview', status: 'doing' }], interaction: { drag_drop: 'planned', edit_cards: 'metadata-only' } } } },
          { id: 'kanban-done', kind: 'kanban-column', title: 'Done', metadata: { kanban: { status: 'board-ready', column: 'Done', color: 'green', cards: [{ id: 'card-install', title: 'Install board template', status: 'done' }], interaction: { drag_drop: 'planned', edit_cards: 'metadata-only' } } } },
        ], renderer: '<script>bad()</script>', api_key: 'UNTRUSTED_VALUE' } },
        { ok: true, demo: 'demo_research_harness_pdf_export', template: 'research', mode: 'metadata-only-smoke', space: { space_id: 'demo-research-harness-pdf-export', name: 'Research Harness', source: 'UNTRUSTED_SOURCE' }, widget_count: 5, persisted_widget_count: 5, rollback_point: true, persistence_checked: true, queued_event_count: 1, research_rollback_check: { verified: true, restored_event_id: 'rev-before-export', restored_widget_count: 5, replayed_after_restore: true, renderer: '<script>bad()</script>', api_key: 'UNTRUSTED_VALUE' } },
        { ok: true, demo: 'demo_time_travel_restore', template: 'big-bang', mode: 'metadata-only-smoke', space: { space_id: 'demo-time-travel-restore', name: 'Time Travel Smoke', source: 'UNTRUSTED_SOURCE' }, widget_count: 4, persisted_widget_count: 4, rollback_point: true, persistence_checked: true },
      ],
      renderer: '<script>bad()</script>',
      api_key: 'UNTRUSTED_VALUE',
    });
  }
  if (path === 'api/spaces/tool') {
    const body = opts.body ? JSON.parse(opts.body) : {};
    if (body.action === 'space.demo.list') {
      return response({
        ok: true,
        demos: [
          { demo: 'demo_weather_widget', template: 'weather', title: 'Weather answer → persistent widget', mode: 'metadata-only-smoke', renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { demo: 'demo_time_travel_restore', template: 'big-bang', title: 'Time travel rollback', mode: 'metadata-only-smoke', source: 'SECRET_SOURCE' },
        ],
      });
    }
    if (body.action === 'space.demo.run') {
      return response({
        ok: true,
        action: 'space.demo.run',
        demo: body.demo || 'demo_weather_widget',
        template: 'weather',
        mode: 'metadata-only-smoke',
        space: { space_id: 'demo-weather-widget', name: 'Weather Demo Smoke', widget_count: 1, revision_event_id: 'rev-demo', renderer: '<script>bad()</script>', api_key: 'SECRET' },
        widgets: [{ id: 'weather-current', kind: 'weather', title: 'Weather in Prague', renderer: '<script>bad()</script>', api_key: 'SECRET' }],
        widget_count: 1,
        persisted_widget_count: 1,
        persistence_checked: true,
        revision_event_count: 2,
        rollback_point: true,
      });
    }
    if (body.action === 'space.demo.run_all') {
      return response({
        ok: true,
        action: 'space.demo.run_all',
        total: 2,
        passed: 2,
        failed: 0,
        mode: 'metadata-only-smoke',
        results: [
          { ok: true, demo: 'demo_weather_widget', template: 'weather', mode: 'metadata-only-smoke', space: { space_id: 'demo-weather-widget', name: 'Weather Demo Smoke', renderer: '<script>bad()</script>', api_key: 'UNTRUSTED_VALUE' }, widget_count: 1, persisted_widget_count: 1, rollback_point: true, persistence_checked: true },
          { ok: true, demo: 'demo_time_travel_restore', template: 'big-bang', mode: 'metadata-only-smoke', space: { space_id: 'demo-time-travel-restore', name: 'Time Travel Smoke', source: 'UNTRUSTED_SOURCE' }, widget_count: 4, persisted_widget_count: 4, rollback_point: true, persistence_checked: true },
        ],
        renderer: '<script>bad()</script>',
        api_key: 'UNTRUSTED_VALUE',
      });
    }
    if (body.action === 'space.widget.runtime_contract') {
      return response({
        ok: true,
        action: 'space.widget.runtime_contract',
        contract: {
          mode: 'sandbox-contract-draft',
          widget_id: body.widget_id || 'weather',
          execution: 'generated-code-disabled',
          allowed_messages: ['capy:ready', 'capy:resize', 'capy:agent:prompt'],
          blocked_messages: ['capy:raw:eval', 'capy:data:put', 'capy:data:get', 'capy:asset:url'],
          network_policy: { default: 'deny', allowed_schemes: ['https'], agent_mediated: true, renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
          approval_required_for: ['external-navigation', 'network-fetch', 'generated-code-enable', '<script>bad()</script>'],
          renderer: '<script>bad()</script>',
          api_key: 'SECRET_VALUE_DO_NOT_LEAK',
        },
      });
    }
    if (body.action === 'space.creator.preview') {
      if (scenario === 'creatorPreviewUnsafeIds') {
        return response({
          ok: true,
          action: 'space.creator.preview',
          stored: false,
          executed: false,
          stage: 'sandbox-preview-required',
          preview_id: 'preview/../escape',
          gates: {
            sandbox_preview_required: true,
            visual_qa_required: true,
            approve_commit_required: true,
          },
          spec: {
            space: { space_id: 'creator/../lab', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
            widgets: [
              { id: '../widget', kind: 'status', title: 'Unsafe Widget', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
            ],
          },
          revision_preview: {
            space_id: 'creator/../lab',
            widget_count: 2,
            widgets: [
              { id: '../widget', kind: 'status', title: 'Safe Widget', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
              { id: 'safe-widget', kind: 'status', title: 'Safe Widget Two', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
            ],
          },
          revision_diff: {
            has_changes: true,
            widgets_to_add: ['../widget', 'safe-widget'],
            widgets_to_remove: ['creator/../old-widget'],
            widgets_to_update: ['safe-update', '../update-widget'],
          },
          raw_prompt: body.prompt,
          generated_code: '<script>bad()</script>',
          api_key: 'SECRET_VALUE_DO_NOT_LEAK',
        });
      }
      if (scenario === 'creatorPreviewExistingSpace' || scenario === 'creatorCommitExistingSpaceReceipt') {
        return response({
          ok: true,
          action: 'space.creator.preview',
          stored: false,
          executed: false,
          stage: 'sandbox-preview-required',
          preview_id: 'preview-existing-safe-1',
          gates: {
            sandbox_preview_required: true,
            visual_qa_required: true,
            approve_commit_required: true,
          },
          space: { space_id: 'existing-creator-lab', name: 'Existing Creator Lab Revised', description: 'Safe revised preview', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
          revision_preview: {
            space_id: 'existing-creator-lab',
            name: 'Existing Creator Lab Revised',
            description: 'Safe revised preview',
            widget_count: 1,
            widgets: [
              { id: 'latest-panel', kind: 'status', title: 'Latest Panel', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
              { id: 'renderer-panel', kind: 'api_key', title: 'SECRET_VALUE_DO_NOT_LEAK', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
            ],
            renderer: '<script>bad()</script>',
            api_key: 'SECRET_VALUE_DO_NOT_LEAK',
          },
          revision_diff: {
            has_changes: true,
            space_fields_to_update: ['description', 'agent_instructions', 'shared_data', 'api_key'],
            widgets_to_add: ['latest-panel'],
            widgets_to_remove: ['old-panel'],
            widgets_to_update: [],
            renderer: '<script>bad()</script>',
            api_key: 'SECRET_VALUE_DO_NOT_LEAK',
          },
          spec: {
            space: { space_id: 'existing-creator-lab', name: 'Existing Creator Lab Revised', description: 'Safe revised preview', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
            widgets: [
              { id: 'latest-panel', kind: 'status', title: 'Latest Panel', metadata: { prompt: 'SECRET_VALUE_DO_NOT_LEAK', renderer: '<script>bad()</script>' }, renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
            ],
          },
          prompt: body.prompt,
          raw_prompt: body.prompt,
          generated_code: '<script>bad()</script>',
          api_key: 'SECRET_VALUE_DO_NOT_LEAK',
        });
      }
      return response({
        ok: true,
        action: 'space.creator.preview',
        stored: false,
        executed: false,
        stage: 'sandbox-preview-required',
        preview_id: 'preview-safe-1',
        gates: {
          sandbox_preview_required: true,
          visual_qa_required: true,
          approve_commit_required: true,
        },
        spec: {
          space: { space_id: 'creator-lab', name: 'Creator Lab <Safe>', description: 'Metadata-only preview', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
          widgets: [
            { id: 'safe-summary', kind: 'markdown', title: 'Summary <Widget>', metadata: { checklist: { items: ['sandbox preview', 'visual QA', 'revision commit'], renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' }, prompt: 'SECRET_VALUE_DO_NOT_LEAK' }, renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
          ],
        },
        prompt: body.prompt,
        raw_prompt: body.prompt,
        generated_code: '<script>bad()</script>',
        api_key: 'SECRET_VALUE_DO_NOT_LEAK',
      });
    }
    if (body.action === 'space.creator.commit') {
      const existingSpaceCommit = scenario === 'creatorCommitExistingSpaceReceipt';
      return response({
        ok: true,
        action: 'space.creator.commit',
        stored: true,
        executed: false,
        stage: 'revisioned-commit',
        space: {
          space_id: existingSpaceCommit ? 'existing-creator-lab' : (scenario === 'creatorCommitUnsafeSpaceId' ? 'creator/../lab' : 'creator-lab'),
          name: existingSpaceCommit ? 'Existing Creator Lab Revised' : 'Creator Lab <Safe>',
          widget_count: 1,
          revision_event_id: 'rev-creator-commit',
          renderer: '<script>bad()</script>',
          api_key: 'SECRET_VALUE_DO_NOT_LEAK'
        },
        revision_preview: existingSpaceCommit ? {
          space_id: 'existing-creator-lab',
          name: 'Existing Creator Lab Revised',
          description: 'Safe committed preview',
          widget_count: 1,
          widgets: [
            { id: 'latest-panel', kind: 'status', title: 'Latest Panel', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
            { id: 'renderer-panel', kind: 'api_key', title: 'SECRET_VALUE_DO_NOT_LEAK', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
          ],
          renderer: '<script>bad()</script>',
          api_key: 'SECRET_VALUE_DO_NOT_LEAK',
        } : undefined,
        revision_diff: existingSpaceCommit ? {
          has_changes: true,
          space_fields_to_update: ['description', 'agent_instructions', 'shared_data', 'api_key'],
          widgets_to_add: ['latest-panel'],
          widgets_to_remove: ['old-panel'],
          widgets_to_update: [],
          renderer: '<script>bad()</script>',
          api_key: 'SECRET_VALUE_DO_NOT_LEAK',
        } : undefined,
        revision_event: { event_id: 'rev-creator-commit', event_type: 'creator.commit', details: { preview_id: body.preview_id, renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } },
      });
    }
  }
  if (path === 'api/spaces/recovery') {
    return response({
      enabled: true,
      generated_widgets_rendered: false,
      safe_admin: {
        metadata_only: true,
        generated_widgets_rendered: false,
        recovery_route: '/api/spaces/recovery',
        restore_routes: ['/api/spaces/revision/restore', '/api/spaces/revision/restore-widget'],
        gate_labels: ['metadata-only recovery', 'generated widgets not rendered', 'rollback controls available', 'disable and repair controls available'],
        renderer: '<script>bad()</script>',
        api_key: 'SECRET_VALUE_DO_NOT_LEAK',
      },
      summary: {
        space_count: 2,
        widget_count: 3,
        disabled_space_count: 1,
        disabled_widget_count: 1,
        rollback_point_count: 2,
        queued_event_count: 1,
        module_count: 3,
        disabled_module_count: 1,
        renderer: '<script>bad()</script>',
        api_key: 'SECRET_VALUE_DO_NOT_LEAK',
      },
      modules: [
        { module_id: 'safe-module', name: 'Safe Module', description: 'Metadata-only module descriptor', scope: 'space', disabled: false, source: 'SECRET_SOURCE', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
        { module_id: 'unsafe-module', name: '[REDACTED]', description: '[REDACTED]', scope: 'global', disabled: true, disabled_reason: '[REDACTED]', revision_event_id: 'module-rev', source: 'SECRET_SOURCE', script: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
        { module_id: 'api_key', name: 'Safe blocked module', description: 'Unsafe id should not become an action target', scope: 'space', disabled: false, revision_event_id: 'unsafe-id-rev' },
      ],
      spaces: [
        {
          space_id: 'broken',
          name: 'Broken <Space>',
          description: 'Recover without <script>running</script>',
          widget_count: 2,
          revision_event_id: 'rev-broken',
          disabled: false,
          disabled_reason: '',
          queued_space_repair_count: 1,
          latest_space_repair_event: { event_id: 'evt-space-repair', event_name: 'agent.repair', status: 'queued', prompt_preview: 'SECRET_VALUE_DO_NOT_LEAK', payload_summary: { api_key: 'SECRET' } },
          renderer: '<script>bad()</script>',
          revisions: [
            { event_id: 'rev-broken', event_type: 'widget.recovery_disabled', space_id: 'broken', created_at: 1710000200, details: { widget_id: 'bad-widget', reason: 'Authorization: Bearer *** renderer: <script>bad()</script>' }, restore_preview: { name: 'Broken current', widget_count: 2, widgets: [{ id: 'bad-widget', title: 'Bad <Widget>', kind: 'html', renderer: '<script>bad()</script>', api_key: 'SECRET' }, { id: 'disabled-widget', title: 'Disabled Widget', kind: 'markdown' }], renderer: '<script>bad()</script>', api_key: 'SECRET' } },
            { event_id: 'rev-before-break', event_type: 'space.updated', space_id: 'broken', created_at: 1710000100, details: { fields: ['widgets'], note: 'safe checkpoint' }, restore_preview: { name: 'Broken safe checkpoint', widget_count: 1, widgets: [{ id: 'safe-widget', title: 'Safe Widget', kind: 'markdown', renderer: '<script>bad()</script>', api_key: 'SECRET' }], renderer: '<script>bad()</script>', api_key: 'SECRET' }, restore_diff: { has_changes: true, widgets_to_update: ['safe-widget', 'raw-html-widget', 'script-widget', 'api_auth_widget', 'source-widget', 'secret-widget'], widgets_to_add: ['added-widget'], widgets_to_remove: ['removed-widget'], renderer: '<script>bad()</script>', api_key: 'SECRET' } },
          ],
          widgets: [
            { id: 'bad-widget', kind: 'html', title: 'Bad <Widget>', disabled: false, renderer: '<script>bad()</script>', queued_event_count: 1, latest_queued_event: { event_id: 'evt-repair', event_name: 'agent.repair', status: 'queued', prompt_preview: 'SECRET_VALUE_DO_NOT_LEAK', payload_summary: { api_key: 'SECRET' } } },
            { id: 'disabled-widget', kind: 'markdown', title: 'Disabled Widget', disabled: true, disabled_reason: 'render failed' },
          ],
        },
        {
          space_id: 'disabled-space',
          name: 'Disabled <Space>',
          description: 'Whole space disabled safely',
          widget_count: 1,
          revision_event_id: 'rev-disabled-space',
          disabled: true,
          disabled_reason: 'shell crash <script>ignored</script>',
          renderer: '<script>bad()</script>',
          widgets: [
            { id: 'still-listed', kind: 'markdown', title: 'Still Listed', disabled: false, renderer: '<script>bad()</script>' },
          ],
        }
      ],
    });
  }
  if (path === 'api/spaces/widgets?space_id=demo-notes-app') {
    return response({ widgets: [
      { id: 'notes-folders', kind: 'folder-list', title: 'Folders', layout: { x: 0, y: 0, w: 5, h: 10, minimized: false }, metadata: { folders: [{ id: 'folder-inbox', title: 'Inbox', api_key: 'SECRET_VALUE_DO_NOT_LEAK' }, { id: 'folder-demo', title: 'Demo Project' }], interaction: { rename: 'metadata-only', create_folder: 'metadata-only', active_folder_id: 'folder-demo', renderer: '<script>bad()</script>' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'notes-editor', kind: 'rich-text-editor', title: 'Editor', layout: { x: 5, y: 0, w: 11, h: 10, minimized: false }, metadata: { notes: { status: 'draft-saved', format: 'markdown', body: 'Demo note draft saved through typed Capy Spaces metadata.', renderer: '<script>bad()</script>', api_key: 'SECRET' } }, renderer: '<script>bad()</script>' },
      { id: 'notes-preview', kind: 'markdown', title: 'Markdown preview', layout: { x: 16, y: 0, w: 8, h: 10, minimized: false }, metadata: { notes: { format: 'markdown', body: '# Demo note\n\nThis markdown preview was saved as metadata-only state.', source: 'SECRET_SOURCE' } } },
      { id: 'notes-attachments', kind: 'attachment-list', title: 'Attachments', layout: { x: 0, y: 10, w: 8, h: 6, minimized: false }, metadata: { attachments: { status: 'agent-mediated', storage: 'agent-mediated', items: [{ id: 'attachment-demo-markdown', name: 'demo-note.md', kind: 'markdown', status: 'ready', api_key: 'SECRET_VALUE_DO_NOT_LEAK' }, { id: 'attachment-whiteboard', name: 'whiteboard.png', kind: 'image', status: 'planned', renderer: '<script>bad()</script>' }] } }, renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
    ] });
  }
  if (path === 'api/spaces/widgets?space_id=demo-kanban-board') {
    return response({ widgets: [
      { id: 'kanban-backlog', kind: 'kanban-column', title: 'Backlog', layout: { x: 0, y: 0, w: 8, h: 8, minimized: false }, metadata: { kanban: { status: 'board-ready', column: 'Backlog', color: 'blue', cards: [{ id: 'card-plan', title: 'Plan the first task', status: 'todo', api_key: 'SECRET_VALUE_DO_NOT_LEAK' }], interaction: { drag_drop: 'planned', edit_cards: 'metadata-only', renderer: '<script>bad()</script>' } } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'kanban-doing', kind: 'kanban-column', title: 'Doing', layout: { x: 8, y: 0, w: 8, h: 8, minimized: false }, metadata: { kanban: { status: 'board-ready', column: 'Doing', color: 'yellow', cards: [{ id: 'card-build', title: 'Build metadata-only board preview', status: 'doing' }], interaction: { drag_drop: 'planned', edit_cards: 'metadata-only' } } }, source: 'SECRET_SOURCE' },
      { id: 'kanban-done', kind: 'kanban-column', title: 'Done', layout: { x: 16, y: 0, w: 8, h: 8, minimized: false }, metadata: { kanban: { status: 'board-ready', column: 'Done', color: 'green', cards: [{ id: 'card-install', title: 'Install board template', status: 'done' }], interaction: { drag_drop: 'planned', edit_cards: 'metadata-only', token: 'SECRET_VALUE_DO_NOT_LEAK' } } } },
      { id: 'kanban-notes', kind: 'markdown', title: 'Board notes', layout: { x: 0, y: 8, w: 24, h: 4, minimized: false }, metadata: { notes: { status: 'ready', summary: 'Demo board state persisted as safe widget metadata.' } } },
    ] });
  }
  if (path === 'api/spaces/widgets?space_id=lab' || path === 'api/spaces/widgets?space_id=demo-weather-widget' || path === 'api/spaces/widgets?space_id=demo-time-travel-restore' || path === 'api/spaces/widgets?space_id=demo-safe-admin-recovery') {
    const minimized = scenario === 'restoreWidget';
    const isDemoWeather = path.indexOf('demo-weather-widget') !== -1;
    const isTimeTravelRestore = path.indexOf('demo-time-travel-restore') !== -1;
    const isRecovery = path.indexOf('demo-safe-admin-recovery') !== -1;
    const widgets = [{
      id: (isDemoWeather || isTimeTravelRestore || isRecovery) ? 'weather-current' : 'weather',
      kind: 'weather',
      title: (isDemoWeather || isTimeTravelRestore || isRecovery) ? 'Weather in Prague' : '<Weather>',
      layout: { x: 12, y: 3, w: 5, h: 4, minimized: minimized },
      recovery: isRecovery ? { disabled: true, disabled_reason: 'demo smoke recovery' } : undefined,
      metadata: {
        weather: {
          location: 'Prague',
          country: 'CZ',
          status: 'observation-ready',
          current: { condition: 'partly cloudy', temperature_c: '18', feels_like_c: '17' },
          summary: 'Partly cloudy in Prague; refreshed through agent-mediated weather metadata.',
          renderer: '<script>bad()</script>',
          api_key: 'SECRET_VALUE_DO_NOT_LEAK',
        },
        prompt: {
          placeholder: 'Ask Capy to refresh or explain the Prague weather widget',
          suggested_event: 'widget.refresh',
          renderer: '<script>bad()</script>',
          api_key: 'SECRET_VALUE_DO_NOT_LEAK',
        },
      },
      renderer: '<script>bad()</script>',
    }];
    if (scenario === 'list' && !isDemoWeather && !isTimeTravelRestore && !isRecovery) {
      widgets.push({
        id: 'safe-label',
        kind: 'markdown',
        title: 'renderer panel SECRET_VALUE_DO_NOT_LEAK api_key',
        layout: { x: 0, y: 8, w: 4, h: 3, minimized: false },
      });
      widgets.push({
        id: 'source-notes',
        kind: 'data-table',
        title: 'Source Notes',
        layout: { x: 4, y: 8, w: 4, h: 3, minimized: false },
      });
      widgets.push({
        id: 'secretary-notes',
        kind: 'tokenization-dashboard',
        title: 'Secretary Cookie Recipes',
        layout: { x: 8, y: 8, w: 4, h: 3, minimized: false },
      });
      widgets.push({
        id: 'generated-panel',
        kind: 'markdown',
        title: 'Generated code raw prompt panel',
        layout: { x: 12, y: 8, w: 4, h: 3, minimized: false },
      });
    }
    return response({ widgets });
  }
  if (path === 'api/spaces/widget/events?space_id=demo-notes-app') {
    return response({ events: [
      { event_id: 'evt-notes-save', event_name: 'notes.save', widget_id: 'notes-editor', status: 'queued', created_at: 1710000200, payload_summary: { action: 'save-note', note: 'bearer placeholder' }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
    ] });
  }
  if (path === 'api/spaces/widget/events?space_id=demo-kanban-board') {
    return response({ events: [
      { event_id: 'evt-kanban-card', event_name: 'kanban.card.move', widget_id: 'kanban-doing', status: 'queued', created_at: 1710000300, payload_summary: { action: 'move-card', card: 'token placeholder' }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
    ] });
  }
  if (path === 'api/spaces/widgets?space_id=demo-snake-iterative-repair') {
    return response({ widgets: [
      { id: 'game-canvas', kind: 'canvas-game', title: 'Snake canvas', layout: { x: 0, y: 0, w: 16, h: 10, minimized: false }, metadata: { game: { title: 'Snake', status: 'generated-code-disabled', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' }, input_policy: { keyboard_focus: 'explicit-click', global_keys: 'blocked' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'game-controls', kind: 'status', title: 'Game controls', layout: { x: 16, y: 0, w: 8, h: 4, minimized: false }, metadata: { controls: { focus: 'explicit-click', restart: 'planned', authorization: 'bearer placeholder' } } },
      { id: 'game-repair-notes', kind: 'markdown', title: 'Repair notes', layout: { x: 16, y: 4, w: 8, h: 6, minimized: false }, metadata: { notes: { status: 'repair-queued', summary: 'Agent repair queued for keyboard focus and collision checks.', source: 'SECRET_SOURCE' } }, source: 'SECRET_SOURCE' },
    ] });
  }
  if (path === 'api/spaces/widget/events?space_id=demo-snake-iterative-repair') {
    return response({ events: [
      { event_id: 'evt-snake-repair', event_name: 'agent.repair', widget_id: 'game-repair-notes', status: 'queued', created_at: 1710000350, payload_summary: { action: 'repair-snake', issue: 'keyboard-focus-and-collision', authorization: 'bearer placeholder' }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
    ] });
  }
  if (path === 'api/spaces/widgets?space_id=demo-daily-dashboard') {
    return response({ widgets: [
      { id: 'dashboard-prices', kind: 'chart', title: 'Market prices', layout: { x: 0, y: 0, w: 8, h: 5, minimized: false }, metadata: { market: { status: 'ready', symbols: ['NVDA', 'AAPL', 'GOOGL'], network: 'agent-mediated', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'dashboard-news', kind: 'news', title: 'News brief', layout: { x: 8, y: 0, w: 8, h: 5, minimized: false }, metadata: { news: { status: 'ready', source: 'agent-mediated', token: 'SECRET_VALUE_DO_NOT_LEAK' } } },
      { id: 'dashboard-agenda', kind: 'agenda', title: 'Today agenda', layout: { x: 16, y: 0, w: 8, h: 5, minimized: false }, metadata: { agenda: { status: 'ready', items: ['Morning brief', 'Market check'], renderer: '<script>bad()</script>' } } },
      { id: 'dashboard-brief', kind: 'markdown', title: 'Daily brief', layout: { x: 0, y: 5, w: 24, h: 4, minimized: false }, metadata: { notes: { status: 'ready', summary: 'Daily dashboard metadata persisted.' } } },
    ] });
  }
  if (path === 'api/spaces/widget/events?space_id=demo-daily-dashboard') {
    return response({ events: [
      { event_id: 'evt-dashboard-refresh', event_name: 'dashboard.refresh', widget_id: 'dashboard-prices', status: 'queued', created_at: 1710000500, payload_summary: { action: 'refresh-dashboard', authorization: 'bearer placeholder' }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
    ] });
  }
  if (path === 'api/spaces/widgets?space_id=demo-camera-dashboard') {
    return response({ widgets: [
      { id: 'camera-grid', kind: 'camera-grid', title: 'Camera grid', layout: { x: 0, y: 0, w: 16, h: 8, minimized: false }, metadata: { cameras: { status: 'approval-required', network: 'agent-mediated', streams: [], renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'camera-permissions', kind: 'status', title: 'Stream permissions', layout: { x: 16, y: 0, w: 8, h: 4, minimized: false }, metadata: { permissions: { camera_urls: 'approval-required', network: 'agent-mediated', authorization: 'bearer placeholder' } } },
      { id: 'camera-incidents', kind: 'table', title: 'Incident notes', layout: { x: 16, y: 4, w: 8, h: 4, minimized: false }, metadata: { incidents: { status: 'empty', rows: [], source: 'SECRET_SOURCE' } }, source: 'SECRET_SOURCE' },
    ] });
  }
  if (path === 'api/spaces/widget/events?space_id=demo-camera-dashboard') {
    return response({ events: [] });
  }
  if (path === 'api/spaces/widgets?space_id=demo-stock-chart') {
    return response({ widgets: [
      { id: 'stock-chart', kind: 'chart', title: 'NVDA / AAPL / GOOGL', layout: { x: 0, y: 0, w: 16, h: 8, minimized: false }, metadata: { market_data: { status: 'market-snapshot-ready', series: ['NVDA', 'AAPL', 'GOOGL'], network: 'agent-mediated', rows: [{ symbol: 'NVDA', last: '905.10', change: '+1.8%', notes: 'GPU demand watch', api_key: 'SECRET_VALUE_DO_NOT_LEAK' }], renderer: '<script>bad()</script>', api_key: 'SECRET' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'stock-watchlist', kind: 'table', title: 'Watchlist', layout: { x: 16, y: 0, w: 8, h: 8, minimized: false }, metadata: { watchlist: { status: 'market-snapshot-ready', rows: [{ symbol: 'AAPL', last: '182.40', change: '-0.3%', notes: 'services margin watch' }], authorization: 'bearer placeholder' } } },
      { id: 'stock-notes', kind: 'markdown', title: 'Market notes', layout: { x: 0, y: 8, w: 24, h: 4, minimized: false }, metadata: { notes: { status: 'ready', summary: 'Demo market snapshot is agent-mediated.' } }, source: 'SECRET_SOURCE' },
    ] });
  }
  if (path === 'api/spaces/widget/events?space_id=demo-stock-chart') {
    return response({ events: [
      { event_id: 'evt-stock-refresh', event_name: 'stock.refresh', widget_id: 'stock-chart', status: 'queued', created_at: 1710000700, payload_summary: { action: 'refresh-market-snapshot', authorization: 'bearer placeholder' }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
    ] });
  }
  if (path === 'api/spaces/widgets?space_id=demo-step-sequencer-piano-roll') {
    return response({ widgets: [
      { id: 'music-sequencer-grid', kind: 'step-sequencer', title: 'Step sequencer', metadata: { status: { pattern: 'demo-pattern-saved', steps: 16 }, audio_policy: { permission: 'explicit-user-gesture', webaudio: 'disabled-until-approved', cleanup: 'planned-on-rerender', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'music-synth-controls', kind: 'audio-controls', title: 'Synth controls', metadata: { audio_policy: { permission: 'explicit-user-gesture', webaudio: 'disabled-until-approved', token: 'SECRET_VALUE_DO_NOT_LEAK' } }, source: 'SECRET_SOURCE' },
      { id: 'music-piano-roll', kind: 'piano-roll', title: 'Piano roll', metadata: { interaction: { keyboard: 'explicit-focus', editing: 'metadata-only', renderer: '<script>bad()</script>' } } },
      { id: 'music-notes', kind: 'markdown', title: 'Music notes', metadata: { notes: { status: 'safe-metadata', summary: 'Piano-roll resize cleanup remains planned.' } } },
    ] });
  }
  if (path === 'api/spaces/widget/events?space_id=demo-step-sequencer-piano-roll') {
    return response({ events: [
      { event_id: 'evt-music-pattern', event_name: 'audio.pattern.save', widget_id: 'music-sequencer-grid', status: 'queued', created_at: 1710000750, payload_summary: { demo: 'demo_step_sequencer_piano_roll', pattern_steps: 16, target: 'sequencer-and-piano-roll', authorization: 'bearer placeholder' }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
    ] });
  }
  if (path === 'api/spaces/widgets?space_id=demo-provider-setup') {
    return response({ widgets: [
      { id: 'model-provider-status', kind: 'status', title: 'Provider status', metadata: { provider: { status: 'review-required', setup: 'external-cli-or-settings', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'model-local-runtime', kind: 'local-runtime', title: 'Local runtime', metadata: { runtime: { status: 'agent-mediated', lmstudio: 'optional', authorization: 'bearer placeholder' } }, source: 'SECRET_SOURCE' },
      { id: 'model-settings-review', kind: 'table', title: 'Settings review', metadata: { settings: { status: 'review-only', fields: ['provider', 'model', 'runtime'], token: 'SECRET_VALUE_DO_NOT_LEAK' } } },
      { id: 'model-next-steps', kind: 'checklist', title: 'Next steps', metadata: { checklist: { items: ['Choose provider', 'Validate model', 'Start first Space'], renderer: '<script>bad()</script>' } } },
    ] });
  }
  if (path === 'api/spaces/widget/events?space_id=demo-provider-setup') {
    return response({ events: [
      { event_id: 'evt-provider-review', event_name: 'provider.setup.review', widget_id: 'model-provider-status', status: 'queued', created_at: 1710000850, payload_summary: { action: 'review-provider-setup', authorization: 'bearer placeholder' }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
    ] });
  }
  if (path === 'api/spaces/widgets?space_id=demo-big-bang-onboarding') {
    return response({ widgets: [
      { id: 'bigbang-welcome', kind: 'markdown', title: 'Welcome to Capy Spaces', layout: { x: 0, y: 0, w: 12, h: 5, minimized: false }, metadata: { notes: { status: 'curated-metadata', summary: 'First-run tour for safe Spaces.', renderer: '<script>bad()</script>' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'bigbang-demo-launcher', kind: 'checklist', title: 'Demo launchers', layout: { x: 12, y: 0, w: 12, h: 5, minimized: false }, metadata: { checklist: { items: ['weather', 'research', 'kanban', 'notes'], api_key: 'SECRET_VALUE_DO_NOT_LEAK' } } },
      { id: 'bigbang-safety', kind: 'status', title: 'Safety guardrails', layout: { x: 0, y: 5, w: 12, h: 4, minimized: false }, metadata: { safety: { generated_code: 'disabled-by-default', recovery: 'available', authorization: 'bearer placeholder' } } },
      { id: 'bigbang-next-steps', kind: 'checklist', title: 'Next steps', layout: { x: 12, y: 5, w: 12, h: 4, minimized: false }, metadata: { checklist: { items: ['Use this space in chat', 'Ask Capy to customize widgets'], renderer: '<script>bad()</script>' } } },
    ] });
  }
  if (path === 'api/spaces/widget/events?space_id=demo-big-bang-onboarding') {
    return response({ events: [] });
  }
  if (path === 'api/spaces/widgets?space_id=demo-local-agent-control-dashboard') {
    return response({ widgets: [
      { id: 'service-api-chat', kind: 'api-connector', title: 'Service API chat', layout: { x: 0, y: 0, w: 10, h: 6, minimized: false }, metadata: { connector: { target: 'local-service', mode: 'agent-mediated', auth: 'configured-outside-widget', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'service-browser-panel', kind: 'browser-surface', title: 'Service browser panel', layout: { x: 10, y: 0, w: 8, h: 6, minimized: false }, metadata: { browser_surface: { url: 'about:blank', inspection: 'metadata-only', approval: 'required', authorization: 'bearer placeholder' } }, source: 'SECRET_SOURCE' },
      { id: 'service-health', kind: 'status', title: 'Health checks', layout: { x: 18, y: 0, w: 6, h: 3, minimized: false }, metadata: { checks: { status: 'pending', endpoints: ['/health', 'api/status'], token: 'SECRET_VALUE_DO_NOT_LEAK' } } },
      { id: 'service-settings-review', kind: 'table', title: 'Settings review', layout: { x: 18, y: 3, w: 6, h: 3, minimized: false }, metadata: { settings: { status: 'review-only', fields: ['provider', 'network', 'auth'], renderer: '<script>bad()</script>' } } },
    ] });
  }
  if (path === 'api/spaces/widget/events?space_id=demo-local-agent-control-dashboard') {
    return response({ events: [
      { event_id: 'evt-service-status', event_name: 'service.status.check', widget_id: 'service-health', status: 'queued', created_at: 1710000800, payload_summary: { action: 'check-local-service', authorization: 'bearer placeholder' }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
    ] });
  }
  if (path === 'api/spaces/widgets?space_id=demo-browser-cocontrol-google-or-test-site') {
    return response({ widgets: [
      { id: 'browser-panel', kind: 'browser-surface', title: 'Shared browser panel', layout: { x: 0, y: 0, w: 16, h: 10, minimized: false }, metadata: { browser_surface: { url: 'about:blank', inspection: 'metadata-only', bridge: 'planned-cdp', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'browser-controls', kind: 'browser-controls', title: 'Agent controls', layout: { x: 16, y: 0, w: 8, h: 5, minimized: false }, metadata: { actions: ['open_url', 'snapshot', 'click_ref', 'type_ref'], permissions: { browser_control: 'agent-mediated', authorization: 'bearer placeholder' } } },
      { id: 'browser-notes', kind: 'markdown', title: 'Browser notes', layout: { x: 16, y: 5, w: 8, h: 5, minimized: false }, metadata: { notes: { status: 'ready', summary: 'Shared browser co-control remains metadata-only.' } }, source: 'SECRET_SOURCE' },
    ] });
  }
  if (path === 'api/spaces/widget/events?space_id=demo-browser-cocontrol-google-or-test-site') {
    return response({ events: [
      { event_id: 'evt-browser-open', event_name: 'browser.open_url', widget_id: 'browser-panel', status: 'queued', created_at: 1710000600, payload_summary: { action: 'open-test-site', authorization: 'bearer placeholder' }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
    ] });
  }
  if (path === 'api/spaces/widgets?space_id=demo-research-harness-pdf-export') {
    return response({ widgets: [
      { id: 'research-query', kind: 'prompt', title: 'Research query', layout: { x: 0, y: 0, w: 8, h: 4, minimized: false }, metadata: { prompt: { suggested_event: 'agent.prompt', placeholder: 'Ask Capy to research a topic', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { id: 'research-plan', kind: 'status', title: 'Research plan', layout: { x: 8, y: 0, w: 8, h: 4, minimized: false }, metadata: { research: { status: 'planned', phase: 'plan', source: 'SECRET_SOURCE' } }, source: 'SECRET_SOURCE' },
      { id: 'research-sources', kind: 'table', title: 'Sources', layout: { x: 16, y: 0, w: 8, h: 4, minimized: false }, metadata: { research: { status: 'sources-ready', network: 'agent-mediated', api_key: 'SECRET_VALUE_DO_NOT_LEAK' } } },
      { id: 'research-notes', kind: 'markdown', title: 'Notes', layout: { x: 0, y: 4, w: 12, h: 6, minimized: false }, metadata: { research: { status: 'notes-ready', renderer: '<script>bad()</script>' } } },
      { id: 'research-summary', kind: 'markdown', title: 'Summary report', layout: { x: 12, y: 4, w: 12, h: 6, minimized: false }, metadata: { export: { pdf: 'queued', event: 'widget.export.pdf', token: 'SECRET_VALUE_DO_NOT_LEAK' }, research: { status: 'summary-ready' } }, renderer: '<script>bad()</script>' },
    ] });
  }
  if (path === 'api/spaces/widget/events?space_id=demo-research-harness-pdf-export') {
    return response({ events: [
      { event_id: 'evt-research-pdf', event_name: 'widget.export.pdf', widget_id: 'research-summary', status: 'queued', created_at: 1710000400, payload_summary: { action: 'export-pdf', note: 'bearer placeholder' }, prompt_preview: 'Export research markdown without leaking SECRET values', renderer: '<script>bad()</script>', api_key: 'SECRET' },
    ] });
  }
  if (path === 'api/spaces/widget/events?space_id=lab' || path === 'api/spaces/widget/events?space_id=demo-weather-widget' || path === 'api/spaces/widget/events?space_id=demo-time-travel-restore' || path === 'api/spaces/widget/events?space_id=demo-safe-admin-recovery') {
    const isDemoWeather = path.indexOf('demo-weather-widget') !== -1;
    const isTimeTravelRestore = path.indexOf('demo-time-travel-restore') !== -1;
    const isRecovery = path.indexOf('demo-safe-admin-recovery') !== -1;
    const widgetId = (isDemoWeather || isTimeTravelRestore || isRecovery) ? 'weather-current' : 'weather';
    return response({ events: [
      { event_id: 'evt-refresh', event_name: 'widget.refresh', widget_id: widgetId, status: 'queued', created_at: 1710000100, payload_summary: { action: 'refresh', note: 'bearer placeholder' }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { event_id: 'evt-agent', event_name: 'agent.prompt', widget_id: widgetId, status: 'queued', created_at: 1710000000, prompt_preview: 'Use token SECRET_VALUE_DO_NOT_LEAK', payload_summary: { query: 'forecast' } },
    ] });
  }
  if (path === 'api/spaces/widget?space_id=lab&widget_id=weather') {
    return response({ widget: {
      id: 'weather',
      kind: 'markdown',
      title: '<Weather>',
      layout: { x: 12, y: 3, w: 5, h: 4, minimized: false },
      recovery: { disabled: false },
      revision_event_id: 'rev-weather',
      metadata: {
        content_status: 'agent-managed-empty',
        export: { pdf: 'planned' },
        weather: {
          location: 'Prague',
          country: 'CZ',
          units: 'metric',
          status: 'observation-ready',
          current: { condition: 'partly cloudy', temperature_c: '18', feels_like_c: '17' },
          summary: 'Partly cloudy in Prague; refreshed through agent-mediated weather metadata.',
          renderer: '<script>bad()</script>',
          api_key: 'SECRET_VALUE_DO_NOT_LEAK',
        },
        interaction: { refresh: 'agent-mediated', dangerous_html: '<script>bad()</script>' },
        permissions: { network: 'agent-mediated', token: 'SECRET_VALUE_DO_NOT_LEAK', credential: 'SECRET_VALUE_DO_NOT_LEAK' },
        prompt: {
          placeholder: 'Ask Capy to refresh or explain the Prague weather widget',
          suggested_event: 'widget.refresh',
          renderer: '<script>bad()</script>',
          api_key: 'SECRET_VALUE_DO_NOT_LEAK',
        },
      },
      event_bridge: { event_name: 'agent.prompt', status: 'ready-for-user-confirmation', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
      renderer: '<script>bad()</script>',
      html: '<img src=x onerror=bad()>',
      data: { api_key: 'SECRET' },
    } });
  }
  if (path === 'api/spaces/widget?space_id=lab&widget_id=notes-main') {
    return response({ widget: {
      id: 'notes-main',
      kind: 'notes',
      title: 'Daily Notes',
      layout: { x: 0, y: 0, w: 12, h: 8, minimized: false },
      recovery: { disabled: false },
      revision_event_id: 'rev-notes-main',
      metadata: {
        notes: { body: 'Initial notes body', format: 'markdown', updated_by: 'Brendan' },
        permissions: { network: 'none', token: 'SECRET_VALUE_DO_NOT_LEAK' },
      },
      renderer: '<script>bad()</script>',
      api_key: 'SECRET_VALUE_DO_NOT_LEAK',
    } });
  }
  if (path === 'api/spaces/get?space_id=lab') {
    return response({ space: {
      space_id: 'lab',
      name: 'Lab <Detail>',
      description: 'Unsafe <detail>',
      revision_event_id: 'rev1',
      widgets: [{ id: 'weather', kind: 'markdown', title: '<Weather>', layout: { x: 12, y: 3, w: 5, h: 4, minimized: false }, renderer: '<script>bad()</script>' }],
      capabilities: { toolsets: ['web'] },
      recovery: { safe_mode_available: true },
      shared_data: [
        { key: 'research-summary', value_summary: { title: 'Safe research findings', notes: ['ready for widget cooperation'], renderer: '<script>bad()</script>', api_key: 'SECRET' }, metadata_summary: { source_widget: 'weather', authorization: 'Bearer SECRET_VALUE_DO_NOT_LEAK' } },
        { key: 'api_key', value_summary: { note: 'SECRET_VALUE_DO_NOT_LEAK' }, metadata_summary: { renderer: '<script>bad()</script>' } },
      ],
    } });
  }
  if (path === 'api/spaces/revisions?space_id=lab') {
    return response({ revisions: [
      { event_id: 'rev2', event_type: 'widget.updated', space_id: 'lab', created_at: 1710000000, details: { widget_id: 'weather', fields: ['title', 'layout'], note: 'Authorization Bearer SECRET_VALUE_DO_NOT_LEAK', renderer: '<script>bad()</script>', api_key: 'SECRET' }, restore_preview: { name: 'Lab patched', widget_count: 1, widgets: [{ id: 'weather', title: 'Weather patched', kind: 'markdown', renderer: '<script>bad()</script>', api_key: 'SECRET' }], renderer: '<script>bad()</script>', api_key: 'SECRET' }, restore_diff: { has_changes: false, widget_count_delta: 0, widgets_to_add: [], widgets_to_remove: [], widgets_to_update: [], space_fields_to_update: [], renderer: '<script>bad()</script>', api_key: 'SECRET' } },
      { event_id: 'rev1', event_type: 'space.created', space_id: 'lab', created_at: 1709999900, details: { name: 'Lab <Detail>' }, restore_preview: { name: 'Lab <Detail>', widget_count: 1, widgets: [{ id: 'weather', title: '<Weather>', kind: 'markdown', renderer: '<script>bad()</script>', api_key: 'SECRET' }], renderer: '<script>bad()</script>', api_key: 'SECRET' }, restore_diff: { has_changes: true, widget_count_delta: -1, widgets_to_add: [], widgets_to_remove: ['notes'], widgets_to_update: ['weather'], space_fields_to_update: ['description'], renderer: '<script>bad()</script>', api_key: 'SECRET' } },
    ] });
  }
  if (path === 'api/spaces/widget/upsert') {
    return response({ space_id: 'lab', widget: { id: 'notes', kind: 'markdown', title: 'Notes', layout: { x: 2, y: 3, w: 8, h: 5 } }, revision_event_id: 'rev2' });
  }
  if (path === 'api/spaces/widget/patch') {
    return response({ space_id: 'lab', widget: { id: 'weather', kind: 'markdown', title: 'Weather patched', layout: { x: 4, y: 5, w: 9, h: 6 } }, revision_event_id: 'rev-patch', renderer: '<script>bad()</script>' });
  }
  if (path === 'api/spaces/system-widget/upsert') {
    return response({ space_id: 'lab', widget: { id: 'system-chat', kind: 'system', title: 'Chat', layout: { x: 0, y: 0, w: 12, h: 6, minimized: false }, system_panel: 'chat', renderer: '<script>bad()</script>', api_key: 'SECRET' }, revision_event_id: 'rev-system' });
  }
  if (path === 'api/spaces/widget/delete') {
    return response({ deleted: true, space_id: 'lab', widget_id: 'weather', revision_event_id: 'rev3' });
  }
  if (path === 'api/spaces/data/delete') {
    return response({ deleted: true, space_id: 'lab', key: 'research-summary', revision_event_id: 'rev-data-delete', renderer: '<script>bad()</script>', api_key: 'SECRET' });
  }
  if (path === 'api/spaces/widget/event') {
    const eventBody = opts.body ? JSON.parse(opts.body) : {};
    return response({ queued: true, space_id: eventBody.space_id || 'lab', widget_id: eventBody.widget_id || 'weather', event_name: eventBody.event_name || 'agent.prompt', event_id: 'evt1', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' });
  }
  if (path === 'api/spaces/recovery/disable-widget') {
    return response({ disabled: true, space_id: 'broken', widget_id: 'bad-widget', revision_event_id: 'rev-disable' });
  }
  if (path === 'api/spaces/recovery/enable-widget') {
    return response({ disabled: false, space_id: 'broken', widget_id: 'disabled-widget', revision_event_id: 'rev-enable', renderer: '<script>bad()</script>', api_key: 'SECRET' });
  }
  if (path === 'api/spaces/recovery/disable-space') {
    return response({ disabled: true, space_id: 'broken', revision_event_id: 'rev-disable-space', renderer: '<script>bad()</script>', api_key: 'SECRET' });
  }
  if (path === 'api/spaces/recovery/enable-space') {
    return response({ disabled: false, space_id: 'disabled-space', revision_event_id: 'rev-enable-space', renderer: '<script>bad()</script>', api_key: 'SECRET' });
  }
  if (path === 'api/spaces/recovery/disable-module') {
    return response({ disabled: true, module_id: 'safe-module', revision_event_id: 'rev-disable-module', renderer: '<script>bad()</script>', api_key: 'SECRET' });
  }
  if (path === 'api/spaces/recovery/enable-module') {
    return response({ disabled: false, module_id: 'unsafe-module', revision_event_id: 'rev-enable-module', renderer: '<script>bad()</script>', api_key: 'SECRET' });
  }
  if (path === 'api/spaces/create') {
    return response({ space: { space_id: 'ops', name: 'Ops', description: '<b>Operations</b>', widget_count: 0, revision_event_id: 'rev4' } });
  }
  if (path === 'api/spaces/create-from-session') {
    return response({
      ok: true,
      space: { space_id: 'research-chat-space', name: 'Research Chat Space', description: 'Linked chat starter', widget_count: 1, revision_event_id: 'rev-chat', renderer: '<script>bad()</script>', api_key: 'SECRET' },
      session: { session_id: 'session-123', active_space_id: 'research-chat-space' },
    });
  }
  if (path === 'api/spaces/templates/install') {
    const body = opts.body ? JSON.parse(opts.body) : {};
    if (body.template === 'research') {
      return response({
        template: 'research',
        space: { space_id: 'research-harness', name: 'Research Harness', description: 'Research harness starter', widget_count: 5, revision_event_id: 'rev-research' },
        installed_widgets: [
          { id: 'research-query', kind: 'prompt', title: 'Research query', layout: { x: 0, y: 0, w: 8, h: 4, minimized: false }, renderer: '<script>bad()</script>' },
          { id: 'research-plan', kind: 'status', title: 'Plan', layout: { x: 8, y: 0, w: 8, h: 4, minimized: false } },
        ],
      });
    }
    if (body.template === 'dashboard') {
      return response({
        template: 'dashboard',
        space: { space_id: 'daily-dashboard', name: 'Daily Dashboard', description: 'Prices, news, agenda, and briefing starter', widget_count: 4, revision_event_id: 'rev-dashboard' },
        installed_widgets: [
          { id: 'dashboard-prices', kind: 'chart', title: 'Market prices', layout: { x: 0, y: 0, w: 8, h: 5, minimized: false }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { id: 'dashboard-news', kind: 'news', title: 'News brief', layout: { x: 8, y: 0, w: 8, h: 5, minimized: false } },
        ],
      });
    }
    if (body.template === 'kanban') {
      return response({
        template: 'kanban',
        space: { space_id: 'kanban-board', name: 'Kanban Board', description: 'Colorful board starter', widget_count: 4, revision_event_id: 'rev-kanban' },
        installed_widgets: [
          { id: 'kanban-backlog', kind: 'kanban-column', title: 'Backlog', layout: { x: 0, y: 0, w: 8, h: 8, minimized: false }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { id: 'kanban-doing', kind: 'kanban-column', title: 'Doing', layout: { x: 8, y: 0, w: 8, h: 8, minimized: false } },
        ],
      });
    }
    if (body.template === 'notes') {
      return response({
        template: 'notes',
        space: { space_id: 'notes-app', name: 'Notes App', description: 'Metadata-only notes starter', widget_count: 4, revision_event_id: 'rev-notes' },
        installed_widgets: [
          { id: 'notes-folders', kind: 'folder-list', title: 'Folders', layout: { x: 0, y: 0, w: 5, h: 10, minimized: false }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { id: 'notes-editor', kind: 'rich-text-editor', title: 'Editor', layout: { x: 5, y: 0, w: 11, h: 10, minimized: false } },
        ],
      });
    }
    if (body.template === 'browser') {
      return response({
        template: 'browser',
        space: { space_id: 'browser-surface', name: 'Browser Surface', description: 'Inspectable browser panel starter', widget_count: 3, revision_event_id: 'rev-browser' },
        installed_widgets: [
          { id: 'browser-panel', kind: 'browser-surface', title: 'Shared browser panel', layout: { x: 0, y: 0, w: 16, h: 10, minimized: false }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { id: 'browser-controls', kind: 'browser-controls', title: 'Agent controls', layout: { x: 16, y: 0, w: 8, h: 5, minimized: false } },
        ],
      });
    }
    if (body.template === 'stock') {
      return response({
        template: 'stock',
        space: { space_id: 'stock-chart', name: 'Stock Chart', description: 'Safe market chart starter', widget_count: 3, revision_event_id: 'rev-stock' },
        installed_widgets: [
          { id: 'stock-chart', kind: 'chart', title: 'NVDA / AAPL / GOOGL', layout: { x: 0, y: 0, w: 16, h: 8, minimized: false }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { id: 'stock-watchlist', kind: 'table', title: 'Watchlist', layout: { x: 16, y: 0, w: 8, h: 8, minimized: false }, source: 'SECRET_SOURCE' },
          { id: 'stock-notes', kind: 'markdown', title: 'Market notes', layout: { x: 0, y: 8, w: 24, h: 4, minimized: false } },
        ],
      });
    }
    if (body.template === 'camera') {
      return response({
        template: 'camera',
        space: { space_id: 'camera-dashboard', name: 'Camera Dashboard', description: 'Safe stream review starter', widget_count: 3, revision_event_id: 'rev-camera' },
        installed_widgets: [
          { id: 'camera-grid', kind: 'camera-grid', title: 'Camera grid', layout: { x: 0, y: 0, w: 16, h: 10, minimized: false }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { id: 'camera-permissions', kind: 'status', title: 'Stream permissions', layout: { x: 16, y: 0, w: 8, h: 5, minimized: false }, source: 'SECRET_SOURCE' },
          { id: 'camera-incidents', kind: 'table', title: 'Incident notes', layout: { x: 16, y: 5, w: 8, h: 5, minimized: false } },
        ],
      });
    }
    if (body.template === 'big-bang') {
      return response({
        template: 'big-bang',
        space: { space_id: 'big-bang-onboarding', name: 'Big Bang Onboarding', description: 'Safe first-run tour starter', widget_count: 4, revision_event_id: 'rev-bigbang' },
        installed_widgets: [
          { id: 'bigbang-welcome', kind: 'markdown', title: 'Welcome to Capy Spaces', layout: { x: 0, y: 0, w: 12, h: 5, minimized: false }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { id: 'bigbang-demo-launcher', kind: 'checklist', title: 'Demo launchers', layout: { x: 12, y: 0, w: 12, h: 5, minimized: false }, source: 'SECRET_SOURCE' },
          { id: 'bigbang-safety', kind: 'status', title: 'Safety guardrails', layout: { x: 0, y: 5, w: 12, h: 4, minimized: false } },
          { id: 'bigbang-next-steps', kind: 'checklist', title: 'Next steps', layout: { x: 12, y: 5, w: 12, h: 4, minimized: false } },
        ],
      });
    }
    if (body.template === 'game') {
      return response({
        template: 'game',
        space: { space_id: 'game-sandbox', name: 'Game Sandbox', description: 'Safe snake/canvas starter', widget_count: 3, revision_event_id: 'rev-game' },
        installed_widgets: [
          { id: 'game-canvas', kind: 'canvas-game', title: 'Snake game sandbox', layout: { x: 0, y: 0, w: 16, h: 10, minimized: false }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { id: 'game-controls', kind: 'status', title: 'Game controls', layout: { x: 16, y: 0, w: 8, h: 5, minimized: false }, source: 'SECRET_SOURCE' },
          { id: 'game-repair-notes', kind: 'markdown', title: 'Repair notes', layout: { x: 16, y: 5, w: 8, h: 5, minimized: false } },
        ],
      });
    }
    if (body.template === 'music') {
      return response({
        template: 'music',
        space: { space_id: 'music-sequencer', name: 'Music Sequencer', description: 'Safe WebAudio sequencer starter', widget_count: 4, revision_event_id: 'rev-music' },
        installed_widgets: [
          { id: 'music-sequencer-grid', kind: 'step-sequencer', title: 'Step sequencer', layout: { x: 0, y: 0, w: 14, h: 8, minimized: false }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { id: 'music-synth-controls', kind: 'audio-controls', title: 'Synth controls', layout: { x: 14, y: 0, w: 10, h: 4, minimized: false }, source: 'SECRET_SOURCE' },
          { id: 'music-piano-roll', kind: 'piano-roll', title: 'Piano roll', layout: { x: 0, y: 8, w: 18, h: 6, minimized: false } },
          { id: 'music-notes', kind: 'markdown', title: 'Music notes', layout: { x: 18, y: 8, w: 6, h: 6, minimized: false } },
        ],
      });
    }
    if (body.template === 'service') {
      return response({
        template: 'service',
        space: { space_id: 'local-service-dashboard', name: 'Local Service Dashboard', description: 'Safe local service/API dashboard starter', widget_count: 4, revision_event_id: 'rev-service' },
        installed_widgets: [
          { id: 'service-api-chat', kind: 'api-connector', title: 'Service API chat', layout: { x: 0, y: 0, w: 10, h: 6, minimized: false }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { id: 'service-browser-panel', kind: 'browser-surface', title: 'Service browser panel', layout: { x: 10, y: 0, w: 14, h: 8, minimized: false }, source: 'SECRET_SOURCE' },
          { id: 'service-health', kind: 'status', title: 'Health checks', layout: { x: 0, y: 6, w: 10, h: 4, minimized: false }, token: 'SECRET_TOKEN' },
          { id: 'service-settings-review', kind: 'table', title: 'Settings review', layout: { x: 10, y: 8, w: 14, h: 4, minimized: false } },
        ],
      });
    }
    if (body.template === 'model-setup') {
      return response({
        template: 'model-setup',
        space: { space_id: 'model-provider-setup', name: 'Model Provider Setup', description: 'Safe provider/local-model setup starter', widget_count: 4, revision_event_id: 'rev-model' },
        installed_widgets: [
          { id: 'model-provider-status', kind: 'status', title: 'Provider status', layout: { x: 0, y: 0, w: 10, h: 5, minimized: false }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { id: 'model-local-runtime', kind: 'local-runtime', title: 'Local runtime', layout: { x: 10, y: 0, w: 8, h: 5, minimized: false }, source: 'SECRET_SOURCE' },
          { id: 'model-settings-review', kind: 'table', title: 'Settings review', layout: { x: 18, y: 0, w: 6, h: 5, minimized: false }, token: 'SECRET_TOKEN' },
          { id: 'model-next-steps', kind: 'checklist', title: 'Next steps', layout: { x: 0, y: 5, w: 24, h: 4, minimized: false } },
        ],
      });
    }
    return response({
      template: 'weather',
      space: { space_id: 'weather-demo', name: 'Weather Demo', description: 'Prague weather starter', widget_count: 1, revision_event_id: 'rev-weather' },
      installed_widgets: [{ id: 'weather-current', kind: 'weather', title: 'Weather in Prague', layout: { x: 0, y: 0, w: 8, h: 5, minimized: false }, renderer: '<script>bad()</script>' }],
    });
  }
  if (path === 'api/spaces/templates/reset') {
    const body = opts.body ? JSON.parse(opts.body) : {};
    return response({
      template: body.template || 'big-bang',
      reset: true,
      space: { space_id: body.space_id || 'big-bang-onboarding', name: 'Big Bang Onboarding', description: 'Safe reset tour starter', widget_count: 4, revision_event_id: 'rev-reset-bigbang', renderer: '<script>bad()</script>', api_key: 'SECRET' },
      installed_widgets: [
        { id: 'bigbang-welcome', kind: 'markdown', title: 'Welcome to Capy Spaces', renderer: '<script>bad()</script>', api_key: 'SECRET' },
        { id: 'bigbang-demo-launcher', kind: 'checklist', title: 'Demo launchers', source: 'SECRET_SOURCE' },
        { id: 'bigbang-safety', kind: 'status', title: 'Safety guardrails' },
        { id: 'bigbang-next-steps', kind: 'checklist', title: 'Next steps' },
      ],
    });
  }
  if (path === 'api/spaces/revision/restore') {
    const body = opts.body ? JSON.parse(opts.body) : {};
    return response({
      ok: true,
      space: { space_id: body.space_id || 'lab', name: 'Lab restored', description: 'Restored safely', widgets: [{ id: 'weather', kind: 'markdown', title: 'Weather restored', renderer: '<script>bad()</script>', api_key: 'SECRET' }], revision_event_id: 'rev-restore' },
      restored_event_id: body.event_id || 'rev1',
      revision_event_id: 'rev-restore',
      renderer: '<script>bad()</script>',
      api_key: 'SECRET',
    });
  }
  if (path === 'api/spaces/revision/restore-widget') {
    const body = opts.body ? JSON.parse(opts.body) : {};
    return response({
      ok: true,
      space_id: body.space_id || 'lab',
      widget: { id: body.widget_id || 'weather', kind: 'markdown', title: 'Weather restored', renderer: '<script>bad()</script>', api_key: 'SECRET' },
      restored_event_id: body.event_id || 'rev1',
      revision_event_id: 'rev-widget-restore',
      renderer: '<script>bad()</script>',
      api_key: 'SECRET',
    });
  }
  if (path === 'api/spaces/recovery/repair-space') {
    const body = opts.body ? JSON.parse(opts.body) : {};
    return response({
      queued: true,
      status: 'queued',
      space_id: body.space_id || 'broken',
      event_name: 'agent.repair',
      event_id: 'evt-space-repair',
      prompt_preview: '[REDACTED]',
      payload_summary: { action: 'repair-space', renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
      renderer: '<script>bad()</script>',
      api_key: 'SECRET_VALUE_DO_NOT_LEAK',
    });
  }
  if (path === 'api/spaces/update') {
    return response({ space: { space_id: 'lab', name: 'Lab Edited', description: 'Updated', widget_count: 1, revision_event_id: 'rev5' } });
  }
  if (path === 'api/spaces/delete') {
    return response({ deleted: true, space_id: 'lab', revision_event_id: 'rev6' });
  }
  if (path === 'api/spaces/activate') {
    return response({ ok: true, session: { session_id: 'session-123', active_space_id: 'lab' } });
  }
  if (path === 'api/spaces/deactivate') {
    return response({ ok: true, session: { session_id: 'session-123', active_space_id: null } });
  }
  if (path === 'api/spaces/export') {
    const body = opts.body ? JSON.parse(opts.body) : {};
    return response({
      ok: true,
      space_id: body.space_id || 'lab',
      format: body.format || 'yaml',
      filename: (body.space_id || 'lab') + '-space-agent.' + (body.format === 'zip' ? 'zip' : 'yaml'),
      space_yaml: 'id: lab\nname: Lab\nrenderer: <script>bad()</script>\napi_key: SECRET',
      widgets: {'widgets/weather.yaml': 'id: weather\nscript: <script>bad()</script>\ntoken: SECRET'},
      archive_b64: body.format === 'zip' ? 'U0VDUkVUX0FSQ0hJVkVfSU1BR0lOQVJZ=' : undefined,
      zip_b64: body.format === 'zip' ? 'U0VDUkVUX1pJUF9JTUFHSU5BUlk=' : undefined,
    });
  }
  if (path === 'api/spaces/import') {
    const body = opts.body ? JSON.parse(opts.body) : {};
    const isZip = !!body.archive_b64;
    if (scenario === 'importSpaceAgentHostileYaml') {
      return response({
        ok: true,
        source: 'space-agent-yaml',
        space: { space_id: 'imported-lab', name: 'SECRET_VALUE_DO_NOT_LEAK renderer panel', description: 'Imported safely', widget_count: 2, revision_event_id: 'rev-import' },
        imported_widgets: [
          { id: 'weather', kind: 'html', title: 'Weather', renderer: '<script>bad()</script>', api_key: 'SECRET' },
          { id: 'renderer-panel', kind: 'api_key', type: 'source', title: 'SECRET_VALUE_DO_NOT_LEAK <script>bad()</script>' },
        ],
        warnings: [
          { type: 'unsupported_space_agent_api', file: 'widgets/weather.yaml', api: 'space.current.widget.patch', message: 'Unsupported Space Agent API reference omitted during import.' },
          { type: 'unsupported_space_agent_api', file: 'widgets/source.yaml', api: 'api_auth', message: 'renderer SOURCE_LEAK_SENTINEL DATA_LEAK_SENTINEL generated_code raw_prompt <script>bad()</script> SECRET_VALUE_DO_NOT_LEAK' },
        ],
        space_yaml: body.space_yaml,
        widgets: body.widgets,
        archive_b64: body.archive_b64,
      });
    }
    return response({
      ok: true,
      source: isZip ? 'space-agent-zip' : 'space-agent-yaml',
      space: { space_id: isZip ? 'imported-zip-lab' : 'imported-lab', name: isZip ? 'Imported ZIP Lab' : 'Imported Lab', description: 'Imported safely', widget_count: 1, revision_event_id: 'rev-import' },
      imported_widgets: [{ id: 'weather', kind: 'html', title: 'Weather', renderer: '<script>bad()</script>', api_key: 'SECRET' }],
      warnings: [{ type: 'unsupported_space_agent_api', file: 'widgets/weather.yaml', api: 'space.current.widget.patch', message: 'Unsupported Space Agent API reference omitted during import.', renderer: '<script>bad()</script>', api_key: 'SECRET' }],
      space_yaml: body.space_yaml,
      widgets: body.widgets,
      archive_b64: body.archive_b64,
    });
  }
  throw new Error('unexpected fetch path: ' + path);
};

vm.runInThisContext(src, { filename: 'spaces.js' });
const root = makeElement('capySpacesRoot');

async function click(action, dataset) {
  const listener = root.listeners.click;
  if (!listener) throw new Error('click listener not registered');
  await listener({
    target: {
      closest(selector) {
        if (selector !== '[data-capy-action]') return null;
        return { dataset: Object.assign({ capyAction: action }, dataset || {}) };
      }
    }
  });
}

async function dispatchWindowMessage(data, opts) {
  const listener = windowListeners.message;
  if (!listener) return;
  opts = opts || {};
  await listener({ data, origin: opts.origin || 'null', source: opts.source || { mockFrame: true } });
}

(async () => {
  if (scenario === 'list') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
  } else if (scenario === 'save') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await click('saveWidget', { spaceId: 'lab' });
  } else if (scenario === 'delete') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await click('deleteWidget', { spaceId: 'lab', widgetId: 'weather' });
  } else if (scenario === 'deleteWidgetConfirmed') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await click('deleteWidget', { spaceId: 'lab', widgetId: 'weather' });
  } else if (scenario === 'deleteWidgetCancelled') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return false; };
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await click('deleteWidget', { spaceId: 'lab', widgetId: 'weather' });
  } else if (scenario === 'editWidget') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('editWidget', { spaceId: 'lab', widgetId: 'weather', widgetTitle: '<Weather>', widgetKind: 'markdown', widgetX: '12', widgetY: '3', widgetW: '5', widgetH: '4' });
  } else if (scenario === 'viewWidgetDetails') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    beforeHtml = root.innerHTML;
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
  } else if (scenario === 'runtimePromptMessage') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    const match = root.innerHTML.match(/data-runtime-token="([^"]+)"/);
    if (!match) throw new Error('runtime token missing from widget detail shell');
    await dispatchWindowMessage({
      type: 'capy:agent:prompt',
      runtime_token: match[1],
      space_id: 'lab',
      widget_id: 'weather',
      prompt: 'Refresh safely without SECRET_VALUE_DO_NOT_LEAK or <script>bad()</script>',
      payload: { renderer: '<script>bad()</script>', api_key: 'SECRET_VALUE_DO_NOT_LEAK', action: 'prompt' },
    });
  } else if (scenario === 'runtimeBlockedMessage') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    const match = root.innerHTML.match(/data-runtime-token="([^"]+)"/);
    if (!match) throw new Error('runtime token missing from widget detail shell');
    await dispatchWindowMessage({
      type: 'capy:raw:eval',
      runtime_token: match[1],
      space_id: 'lab',
      widget_id: 'weather',
      code: 'alert(SECRET_VALUE_DO_NOT_LEAK)',
      renderer: '<script>bad()</script>',
    });
  } else if (scenario === 'runtimeConflictingMessageType') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    const match = root.innerHTML.match(/data-runtime-token="([^"]+)"/);
    if (!match) throw new Error('runtime token missing from widget detail shell');
    await dispatchWindowMessage({
      type: 'capy:agent:prompt',
      message_type: 'capy:raw:eval',
      runtime_token: match[1],
      space_id: 'lab',
      widget_id: 'weather',
      prompt: 'Queue this benign-looking prompt',
      source: 'eval(SECRET_VALUE_DO_NOT_LEAK)',
      renderer: '<script>bad()</script>',
    });
  } else if (scenario === 'runtimePromptCancelled') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return false; };
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    const match = root.innerHTML.match(/data-runtime-token="([^"]+)"/);
    if (!match) throw new Error('runtime token missing from widget detail shell');
    await dispatchWindowMessage({ type: 'capy:agent:prompt', runtime_token: match[1], space_id: 'lab', widget_id: 'weather', prompt: 'Refresh safely' });
  } else if (scenario === 'runtimePromptForeignOrigin') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    const match = root.innerHTML.match(/data-runtime-token="([^"]+)"/);
    if (!match) throw new Error('runtime token missing from widget detail shell');
    await dispatchWindowMessage({
      type: 'capy:agent:prompt',
      runtime_token: match[1],
      space_id: 'lab',
      widget_id: 'weather',
      prompt: 'Refresh safely',
    }, { origin: 'https://evil.example' });
  } else if (scenario === 'runtimePromptStaleShell') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    const match = root.innerHTML.match(/data-runtime-token="([^"]+)"/);
    if (!match) throw new Error('runtime token missing from widget detail shell');
    root.innerHTML = '<div class="capy-spaces-card">navigated away</div>';
    await dispatchWindowMessage({ type: 'capy:agent:prompt', runtime_token: match[1], space_id: 'lab', widget_id: 'weather', prompt: 'Refresh safely' });
  } else if (scenario === 'runtimeSensitivePromptMessage') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    const match = root.innerHTML.match(/data-runtime-token="([^"]+)"/);
    if (!match) throw new Error('runtime token missing from widget detail shell');
    await dispatchWindowMessage({
      type: 'capy:agent:prompt',
      runtime_token: match[1],
      space_id: 'lab',
      widget_id: 'weather',
      prompt: 'Check access_token=TOKEN_VALUE api_auth=Bearer cookie=session credential=abc source=raw html=<img onerror=alert(1)> javascript:bad() SECRET_VALUE_DO_NOT_LEAK',
    });
  } else if (scenario === 'runtimeCodeLikeSensitivePrompt') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    const match = root.innerHTML.match(/data-runtime-token="([^"]+)"/);
    if (!match) throw new Error('runtime token missing from widget detail shell');
    await dispatchWindowMessage({
      type: 'capy:agent:prompt',
      runtime_token: match[1],
      space_id: 'lab',
      widget_id: 'weather',
      prompt: 'source = function bad() { return api_auth: Bearer abcdef; } data = {"cookie":"session abc def", "ok": true }',
    });
  } else if (scenario === 'runtimeGeneratedAuthPromptMarkers') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    const match = root.innerHTML.match(/data-runtime-token=\"([^\"]+)\"/);
    if (!match) throw new Error('runtime token missing from widget detail shell');
    await dispatchWindowMessage({
      type: 'capy:agent:prompt',
      runtime_token: match[1],
      space_id: 'lab',
      widget_id: 'weather',
      prompt: 'auth = sk-TESTSECRETLOOKING1234567890 prompt = hidden generated-code: function render(){ return "unsafe" }',
    });
  } else if (scenario === 'runtimeBlockedMutationMessages') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    const match = root.innerHTML.match(/data-runtime-token=\"([^\"]+)\"/);
    if (!match) throw new Error('runtime token missing from widget detail shell');
    await dispatchWindowMessage({ type: 'capy:data:put', runtime_token: match[1], space_id: 'lab', widget_id: 'weather', data: { cookie: 'session abc def' } });
    await dispatchWindowMessage({ type: 'capy:eval:run', runtime_token: match[1], space_id: 'lab', widget_id: 'weather', source: 'eval(SECRET_VALUE_DO_NOT_LEAK)' });
    await dispatchWindowMessage({ type: 'capy:raw:source', runtime_token: match[1], space_id: 'lab', widget_id: 'weather', renderer: '<script>bad()</script>' });
  } else if (scenario === 'runtimeBlockedReadMessages') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    const match = root.innerHTML.match(/data-runtime-token=\"([^\"]+)\"/);
    if (!match) throw new Error('runtime token missing from widget detail shell');
    await dispatchWindowMessage({
      type: 'capy:data:get',
      runtime_token: match[1],
      space_id: 'lab',
      widget_id: 'weather',
      key: 'shared_data.private_weather_token',
      path: 'spaces/lab/widgets/weather/data.json',
      payload: { authorization: 'Bearer SECRET_VALUE_DO_NOT_LEAK', cookie: 'session abc def' },
      renderer: '<script>bad()</script>',
      source: 'SECRET_SOURCE',
    });
    await dispatchWindowMessage({
      message_type: 'capy:asset:url',
      runtime_token: match[1],
      space_id: 'lab',
      widget_id: 'weather',
      asset_id: 'weather-map',
      path: 'assets/private/weather-map.png?token=SECRET_VALUE_DO_NOT_LEAK',
      url: 'https://asset.example/private/weather-map.png?authorization=Bearer SECRET_VALUE_DO_NOT_LEAK',
      html: '<img src=x onerror=alert(1)>',
      api_key: 'SECRET_VALUE_DO_NOT_LEAK',
    });
  } else if (scenario === 'runtimeBlockedHostileTypeReflection') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    const match = root.innerHTML.match(/data-runtime-token=\"([^\"]+)\"/);
    if (!match) throw new Error('runtime token missing from widget detail shell');
    await dispatchWindowMessage({
      type: 'capy:raw:SECRET_VALUE_DO_NOT_LEAK',
      runtime_token: match[1],
      space_id: 'lab',
      widget_id: 'weather',
      renderer: '<script>bad()</script>',
      source: 'SECRET_SOURCE',
      payload: { api_key: 'SECRET_VALUE_DO_NOT_LEAK' },
    });
  } else if (scenario === 'runtimeTokenRotates') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    const firstMatch = root.innerHTML.match(/data-runtime-token="([^"]+)"/);
    if (!firstMatch) throw new Error('first runtime token missing from widget detail shell');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    const secondMatch = root.innerHTML.match(/data-runtime-token="([^"]+)"/);
    if (!secondMatch) throw new Error('second runtime token missing from widget detail shell');
    await dispatchWindowMessage({ type: 'capy:agent:prompt', runtime_token: firstMatch[1], space_id: 'lab', widget_id: 'weather', prompt: 'Refresh safely' });
    root.dataset.firstRuntimeToken = firstMatch[1];
    root.dataset.secondRuntimeToken = secondMatch[1];
  } else if (scenario === 'requestWidgetPdfExport') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    await click('requestWidgetPdfExport', { spaceId: 'lab', widgetId: 'weather', widgetTitle: '<Weather>' });
  } else if (scenario === 'saveNotesWidget') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'notes-main' });
    beforeHtml = root.innerHTML;
    values['#capyWidgetNotesBody'] = 'Updated real note\n\n- persists through widget patch';
    await click('saveWidgetNotes', { spaceId: 'lab', widgetId: 'notes-main' });
  } else if (scenario === 'editWidgetSave') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('editWidget', { spaceId: 'lab', widgetId: 'weather', widgetTitle: '<Weather>', widgetKind: 'markdown', widgetX: '12', widgetY: '3', widgetW: '5', widgetH: '4' });
    values['#capyWidgetTitle'] = 'Weather patched';
    values['#capyWidgetKind'] = 'markdown';
    values['#capyWidgetX'] = '4';
    values['#capyWidgetY'] = '5';
    values['#capyWidgetW'] = '9';
    values['#capyWidgetH'] = '6';
    await click('saveWidget', { spaceId: 'lab' });
  } else if (scenario === 'moveWidgetLeft') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    beforeHtml = root.innerHTML;
    await click('moveWidget', { spaceId: 'lab', widgetId: 'weather', widgetX: '12', widgetY: '3', widgetW: '5', widgetH: '4', moveDx: '-1', moveDy: '0' });
  } else if (scenario === 'resizeWidgetWider') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    beforeHtml = root.innerHTML;
    await click('resizeWidget', { spaceId: 'lab', widgetId: 'weather', widgetX: '12', widgetY: '3', widgetW: '5', widgetH: '4', resizeDw: '1', resizeDh: '0' });
  } else if (scenario === 'minimizeWidget') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    beforeHtml = root.innerHTML;
    await click('toggleWidgetMinimized', { spaceId: 'lab', widgetId: 'weather', widgetX: '12', widgetY: '3', widgetW: '5', widgetH: '4', widgetMinimized: 'false' });
  } else if (scenario === 'restoreWidget') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    beforeHtml = root.innerHTML;
    await click('toggleWidgetMinimized', { spaceId: 'lab', widgetId: 'weather', widgetX: '12', widgetY: '3', widgetW: '5', widgetH: '4', widgetMinimized: 'true' });
  } else if (scenario === 'askWidget') {
    global.showPromptDialog = async function(opts) { dialogs.push(opts); return 'Refresh the weather widget'; };
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('askWidget', { spaceId: 'lab', widgetId: 'weather', widgetTitle: '<Weather>' });
  } else if (scenario === 'askWidgetNoPrompt') {
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('askWidget', { spaceId: 'lab', widgetId: 'weather', widgetTitle: '<Weather>' });
  } else if (scenario === 'refreshWidget') {
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    beforeHtml = root.innerHTML;
    await click('refreshWidget', { spaceId: 'lab', widgetId: 'weather', widgetTitle: '<Weather>' });
  } else if (scenario === 'createSpace') {
    await window.loadCapySpaces();
    await click('saveSpace', {});
  } else if (scenario === 'createSpaceFromChat') {
    await window.loadCapySpaces();
    await click('createSpaceFromSession', {});
  } else if (scenario === 'installWeatherDemo') {
    await window.loadCapySpaces();
    await click('installWeatherTemplate', {});
  } else if (scenario === 'installResearchHarness') {
    await window.loadCapySpaces();
    await click('installResearchTemplate', {});
  } else if (scenario === 'installDashboardDemo') {
    await window.loadCapySpaces();
    await click('installDashboardTemplate', {});
  } else if (scenario === 'installKanbanBoard') {
    await window.loadCapySpaces();
    await click('installKanbanTemplate', {});
  } else if (scenario === 'installNotesApp') {
    await window.loadCapySpaces();
    await click('installNotesTemplate', {});
  } else if (scenario === 'installBrowserSurface') {
    await window.loadCapySpaces();
    await click('installBrowserTemplate', {});
  } else if (scenario === 'installStockChart') {
    await window.loadCapySpaces();
    await click('installStockTemplate', {});
  } else if (scenario === 'installCameraDashboard') {
    await window.loadCapySpaces();
    await click('installCameraTemplate', {});
  } else if (scenario === 'installBigBangOnboarding') {
    await window.loadCapySpaces();
    await click('installBigBangTemplate', {});
  } else if (scenario === 'resetBigBangOnboardingConfirmed') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('resetBigBangTemplate', { spaceId: 'big-bang-onboarding' });
  } else if (scenario === 'resetBigBangOnboardingNoDialog') {
    await window.loadCapySpaces();
    await click('resetBigBangTemplate', { spaceId: 'big-bang-onboarding' });
  } else if (scenario === 'resetBigBangOnboardingCancelled') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return false; };
    await window.loadCapySpaces();
    await click('resetBigBangTemplate', { spaceId: 'big-bang-onboarding' });
  } else if (scenario === 'installGameSandbox') {
    await window.loadCapySpaces();
    await click('installGameTemplate', {});
  } else if (scenario === 'installMusicSequencer') {
    await window.loadCapySpaces();
    await click('installMusicTemplate', {});
  } else if (scenario === 'installLocalServiceDashboard') {
    await window.loadCapySpaces();
    await click('installServiceTemplate', {});
  } else if (scenario === 'installModelSetup') {
    await window.loadCapySpaces();
    await click('installModelSetupTemplate', {});
  } else if (scenario === 'runDemoParitySmoke') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runDemoSmoke', { demo: 'demo_weather_widget' });
  } else if (scenario === 'runWeatherWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runWeatherWalkthrough', {});
  } else if (scenario === 'runNotesWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runNotesWalkthrough', {});
  } else if (scenario === 'runKanbanWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runKanbanWalkthrough', {});
  } else if (scenario === 'runSnakeWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runSnakeWalkthrough', {});
  } else if (scenario === 'runDashboardWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runDashboardWalkthrough', {});
  } else if (scenario === 'runCameraWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runCameraWalkthrough', {});
  } else if (scenario === 'runStockWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runStockWalkthrough', {});
  } else if (scenario === 'runLocalServiceWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runLocalServiceWalkthrough', {});
  } else if (scenario === 'runMusicWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runMusicWalkthrough', {});
  } else if (scenario === 'runProviderSetupWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runProviderSetupWalkthrough', {});
  } else if (scenario === 'runBigBangWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runBigBangWalkthrough', {});
  } else if (scenario === 'runTimeTravelWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runTimeTravelWalkthrough', {});
  } else if (scenario === 'runAdminRecoveryWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runAdminRecoveryWalkthrough', {});
  } else if (scenario === 'runBrowserWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runBrowserWalkthrough', {});
  } else if (scenario === 'runResearchWalkthrough') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runResearchWalkthrough', {});
  } else if (scenario === 'runResearchDemoParitySmoke') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runDemoSmoke', { demo: 'demo_research_harness_pdf_export' });
  } else if (scenario === 'runNotesDemoParitySmoke') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runDemoSmoke', { demo: 'demo_notes_app' });
  } else if (scenario === 'runKanbanDemoParitySmoke') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runDemoSmoke', { demo: 'demo_kanban_board' });
  } else if (scenario === 'runDemoParityAllSmokes') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('runAllDemoSmokes', {});
  } else if (scenario === 'openSpaceDetail') {
    await window.loadCapySpaces();
    await click('openSpace', { spaceId: 'lab' });
  } else if (scenario === 'deleteSharedDataNoDialog') {
    await window.loadCapySpaces();
    await click('openSpace', { spaceId: 'lab' });
    beforeHtml = root.innerHTML;
    await click('deleteSharedData', { spaceId: 'lab', dataKey: 'research-summary' });
  } else if (scenario === 'deleteSharedDataCancelled') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return false; };
    await window.loadCapySpaces();
    await click('openSpace', { spaceId: 'lab' });
    beforeHtml = root.innerHTML;
    await click('deleteSharedData', { spaceId: 'lab', dataKey: 'research-summary' });
  } else if (scenario === 'deleteSharedDataConfirmed') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpaces();
    await click('openSpace', { spaceId: 'lab' });
    beforeHtml = root.innerHTML;
    await click('deleteSharedData', { spaceId: 'lab', dataKey: 'research-summary' });
  } else if (scenario === 'restoreRevisionConfirmed') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpaces();
    await click('openSpace', { spaceId: 'lab' });
    beforeHtml = root.innerHTML;
    await click('restoreRevision', { spaceId: 'lab', eventId: 'rev1' });
  } else if (scenario === 'restoreWidgetRevisionConfirmed') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpaces();
    await click('openSpace', { spaceId: 'lab' });
    beforeHtml = root.innerHTML;
    await click('restoreWidgetRevision', { spaceId: 'lab', eventId: 'rev1', widgetId: 'weather' });
  } else if (scenario === 'restoreWidgetRevisionNoDialog') {
    await window.loadCapySpaces();
    await click('openSpace', { spaceId: 'lab' });
    await click('restoreWidgetRevision', { spaceId: 'lab', eventId: 'rev1', widgetId: 'weather' });
  } else if (scenario === 'restoreRevisionNoDialog') {
    await window.loadCapySpaces();
    await click('openSpace', { spaceId: 'lab' });
    await click('restoreRevision', { spaceId: 'lab', eventId: 'rev1' });
  } else if (scenario === 'restoreRevisionCancelled') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return false; };
    await window.loadCapySpaces();
    await click('openSpace', { spaceId: 'lab' });
    await click('restoreRevision', { spaceId: 'lab', eventId: 'rev1' });
  } else if (scenario === 'exportSpaceYaml') {
    await window.loadCapySpaces();
    await click('openSpace', { spaceId: 'lab' });
    await click('exportSpaceYaml', { spaceId: 'lab' });
  } else if (scenario === 'exportSpaceZip') {
    await window.loadCapySpaces();
    await click('openSpace', { spaceId: 'lab' });
    await click('exportSpaceZip', { spaceId: 'lab' });
  } else if (scenario === 'importSpaceAgentYaml') {
    await window.loadCapySpaces();
    await click('importSpaceAgentYaml', {});
  } else if (scenario === 'importSpaceAgentHostileYaml') {
    values['#capySpaceAgentImportSpaceYaml'] = 'id: imported-lab\nname: Imported Lab\ndescription: Imported safely\n';
    values['#capySpaceAgentImportWidgetsJson'] = JSON.stringify({'widgets/weather.yaml': 'id: weather\ntitle: Weather\ntype: html\nrenderer: <script>bad()</script>\napi_key: SECRET'}, null, 2);
    await window.loadCapySpaces();
    await click('importSpaceAgentYaml', {});
  } else if (scenario === 'importSpaceAgentZip') {
    await window.loadCapySpaces();
    await click('importSpaceAgentZip', {});
  } else if (scenario === 'activateSpace') {
    await window.loadCapySpaces();
    await click('activateSpace', { spaceId: 'lab' });
  } else if (scenario === 'clearActiveSpace') {
    global.S.session.active_space_id = 'lab';
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('clearActiveSpace', {});
  } else if (scenario === 'systemWidgetShell') {
    await window.loadCapySpaces();
    await click('openSystemPanel', { systemPanel: 'chat' });
    await click('openSystemPanel', { systemPanel: 'settings' });
  } else if (scenario === 'addSystemWidgetToActiveSpace') {
    global.S.session.active_space_id = 'lab';
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('addSystemWidget', { spaceId: 'lab', systemPanel: 'chat' });
  } else if (scenario === 'editSpace') {
    await window.loadCapySpaces();
    await click('editSpace', { spaceId: 'lab', spaceName: 'Lab Edited', spaceDescription: 'Updated' });
    await click('saveSpace', {});
  } else if (scenario === 'deleteSpace') {
    await window.loadCapySpaces();
    await click('deleteSpace', { spaceId: 'lab' });
  } else if (scenario === 'deleteSpaceConfirmed') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpaces();
    await click('deleteSpace', { spaceId: 'lab' });
  } else if (scenario === 'deleteSpaceCancelled') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return false; };
    await window.loadCapySpaces();
    await click('deleteSpace', { spaceId: 'lab' });
  } else if (scenario === 'recovery') {
    await window.loadCapySpacesRecovery();
  } else if (scenario === 'disableRecoveryWidget') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'disableRecoveryWidget', spaceId: 'broken', widgetId: 'bad-widget' } };
        }
      }
    });
  } else if (scenario === 'disableRecoveryWidgetNoDialog') {
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'disableRecoveryWidget', spaceId: 'broken', widgetId: 'bad-widget' } };
        }
      }
    });
  } else if (scenario === 'disableRecoverySpace') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'disableRecoverySpace', spaceId: 'broken' } };
        }
      }
    });
  } else if (scenario === 'disableRecoverySpaceNoDialog') {
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'disableRecoverySpace', spaceId: 'broken' } };
        }
      }
    });
  } else if (scenario === 'enableRecoveryWidget') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'enableRecoveryWidget', spaceId: 'broken', widgetId: 'disabled-widget' } };
        }
      }
    });
  } else if (scenario === 'enableRecoverySpace') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'enableRecoverySpace', spaceId: 'disabled-space' } };
        }
      }
    });
  } else if (scenario === 'enableRecoverySpaceCancelled') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return false; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'enableRecoverySpace', spaceId: 'disabled-space' } };
        }
      }
    });
  } else if (scenario === 'disableRecoveryModule') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    beforeHtml = makeElement('capySpacesRecovery').innerHTML;
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'disableRecoveryModule', moduleId: 'safe-module' } };
        }
      }
    });
  } else if (scenario === 'disableRecoveryModuleNoDialog') {
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'disableRecoveryModule', moduleId: 'safe-module' } };
        }
      }
    });
  } else if (scenario === 'enableRecoveryModule') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'enableRecoveryModule', moduleId: 'unsafe-module' } };
        }
      }
    });
  } else if (scenario === 'enableRecoveryModuleCancelled') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return false; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'enableRecoveryModule', moduleId: 'unsafe-module' } };
        }
      }
    });
  } else if (scenario === 'enableRecoveryWidgetCancelled') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return false; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'enableRecoveryWidget', spaceId: 'broken', widgetId: 'disabled-widget' } };
        }
      }
    });
  } else if (scenario === 'repairRecoveryWidget') {
    global.showPromptDialog = async function(opts) { dialogs.push(opts); return 'Patch the broken renderer without exposing secrets'; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    beforeHtml = makeElement('capySpacesRecovery').innerHTML;
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'repairRecoveryWidget', spaceId: 'broken', widgetId: 'bad-widget', widgetTitle: 'Bad <Widget>' } };
        }
      }
    });
  } else if (scenario === 'repairRecoveryWidgetNoPrompt') {
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'repairRecoveryWidget', spaceId: 'broken', widgetId: 'bad-widget', widgetTitle: 'Bad <Widget>' } };
        }
      }
    });
  } else if (scenario === 'repairRecoverySpace') {
    global.showPromptDialog = async function(opts) { dialogs.push(opts); return 'Repair the Space shell without exposing renderer secrets'; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    beforeHtml = makeElement('capySpacesRecovery').innerHTML;
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'repairRecoverySpace', spaceId: 'broken' } };
        }
      }
    });
  } else if (scenario === 'repairRecoverySpaceNoPrompt') {
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'repairRecoverySpace', spaceId: 'broken' } };
        }
      }
    });
  } else if (scenario === 'recoveryExportSpaceYaml') {
    await window.loadCapySpacesRecovery();
    beforeHtml = makeElement('capySpacesRecovery').innerHTML;
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'exportRecoverySpaceYaml', spaceId: 'broken' } };
        }
      }
    });
  } else if (scenario === 'recoveryExportSpaceZip') {
    await window.loadCapySpacesRecovery();
    beforeHtml = makeElement('capySpacesRecovery').innerHTML;
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'exportRecoverySpaceZip', spaceId: 'broken' } };
        }
      }
    });
  } else if (scenario === 'restoreRecoveryRevision') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'restoreRecoveryRevision', spaceId: 'broken', eventId: 'rev-before-break' } };
        }
      }
    });
  } else if (scenario === 'restoreRecoveryRevisionNoDialog') {
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'restoreRecoveryRevision', spaceId: 'broken', eventId: 'rev-before-break' } };
        }
      }
    });
  } else if (scenario === 'restoreRecoveryWidgetRevision') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'restoreRecoveryWidgetRevision', spaceId: 'broken', eventId: 'rev-before-break', widgetId: 'safe-widget' } };
        }
      }
    });
  } else if (scenario === 'restoreRecoveryWidgetRevisionNoDialog') {
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'restoreRecoveryWidgetRevision', spaceId: 'broken', eventId: 'rev-before-break', widgetId: 'safe-widget' } };
        }
      }
    });
  } else if (scenario === 'restoreRecoveryWidgetRevisionCancelled') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return false; };
    await window.loadCapySpacesRecovery();
    const listener = makeElement('capySpacesRecovery').listeners.click;
    if (!listener) throw new Error('recovery click listener not registered');
    await listener({
      target: {
        closest(selector) {
          if (selector !== '[data-capy-action]') return null;
          return { dataset: { capyAction: 'restoreRecoveryWidgetRevision', spaceId: 'broken', eventId: 'rev-before-break', widgetId: 'safe-widget' } };
        }
      }
    });
  } else if (scenario === 'creatorPreviewGate' || scenario === 'creatorPreviewUnsafeIds') {
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('previewCreatorSpec', {});
  } else if (scenario === 'creatorPreviewExistingSpace') {
    values['#capyCreatorTargetSpaceId'] = 'existing-creator-lab';
    await window.loadCapySpaces();
    beforeHtml = root.innerHTML;
    await click('previewCreatorSpec', {});
  } else if (scenario === 'creatorCommitExistingSpaceReceipt') {
    values['#capyCreatorTargetSpaceId'] = 'existing-creator-lab';
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpaces();
    await click('previewCreatorSpec', {});
    beforeHtml = root.innerHTML;
    await click('commitCreatorSpec', { previewId: 'preview-existing-safe-1' });
  } else if (scenario === 'creatorCommitConfirmed' || scenario === 'creatorCommitUnsafeSpaceId') {
    global.showConfirmDialog = async function(opts) { dialogs.push(opts); return true; };
    await window.loadCapySpaces();
    await click('previewCreatorSpec', {});
    beforeHtml = root.innerHTML;
    await click('commitCreatorSpec', { previewId: 'preview-safe-1' });
  } else if (scenario === 'creatorCommitNoDialog') {
    await window.loadCapySpaces();
    await click('previewCreatorSpec', {});
    await click('commitCreatorSpec', { previewId: 'preview-safe-1' });
  } else {
    throw new Error('unknown scenario: ' + scenario);
  }
  process.stdout.write(JSON.stringify({ rootHtml: root.innerHTML, beforeHtml, recoveryHtml: makeElement('capySpacesRecovery').innerHTML, recoveryText: makeElement('capySpacesRecovery').textContent, calls, values, rootDataset: root.dataset, dialogs, switchedPanels, capySpaceSyncs }));
})().catch(err => {
  console.error(err && err.stack || String(err));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("spaces_ui_driver") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _run_spaces_scenario(driver_path: str, scenario: str) -> dict:
    result = subprocess.run(
        [NODE, driver_path, str(SPACES_JS_PATH), scenario],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node spaces driver failed: {result.stderr}")
    return json.loads(result.stdout)


def test_spaces_ui_lists_widgets_without_rendering_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "list")

    assert "Weather" in out["rootHtml"]
    assert "&lt;Weather&gt;" in out["rootHtml"]
    assert "Source Notes" in out["rootHtml"]
    assert "data-table · source-notes" in out["rootHtml"]
    assert "Secretary Cookie Recipes" in out["rootHtml"]
    assert "tokenization-dashboard · secretary-notes" in out["rootHtml"]
    assert "[REDACTED]" in out["rootHtml"]
    assert "generated code" not in out["rootHtml"].lower()
    assert "raw prompt" not in out["rootHtml"].lower()
    assert "weather · weather · x12 y3 · 5×4" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]
    assert {"path": "api/spaces/widgets?space_id=lab", "method": "GET", "body": ""} in out["calls"]


def test_spaces_ui_widget_manager_shows_weather_observation_preview(driver_path):
    out = _run_spaces_scenario(driver_path, "list")

    assert "Current weather observation" in out["rootHtml"]
    assert "Prague, CZ" in out["rootHtml"]
    assert "18 °C" in out["rootHtml"]
    assert "Feels like 17 °C" in out["rootHtml"]
    assert "partly cloudy" in out["rootHtml"]
    assert "Observation status: observation-ready" in out["rootHtml"]
    assert "Partly cloudy in Prague; refreshed through agent-mediated weather metadata." in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_widget_manager_shows_weather_prompt_hint(driver_path):
    out = _run_spaces_scenario(driver_path, "list")

    assert "Suggested prompt" in out["rootHtml"]
    assert "Ask Capy to refresh or explain the Prague weather widget" in out["rootHtml"]
    assert "Suggested event: widget.refresh" in out["rootHtml"]
    assert "Metadata-only prompt hint" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_widget_manager_shows_safe_queued_event_inbox(driver_path):
    out = _run_spaces_scenario(driver_path, "list")

    assert {"path": "api/spaces/widget/events?space_id=lab", "method": "GET", "body": ""} in out["calls"]
    assert "Queued widget events" in out["rootHtml"]
    assert "widget.refresh" in out["rootHtml"]
    assert "agent.prompt" in out["rootHtml"]
    assert "weather · queued" in out["rootHtml"]
    assert "Event: evt-refresh" in out["rootHtml"]
    assert "2024-03-09 16:01:40 UTC" in out["rootHtml"]
    assert "action: refresh" in out["rootHtml"]
    assert "note: [REDACTED]" in out["rootHtml"]
    assert "prompt: [REDACTED]" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET_VALUE_DO_NOT_LEAK" not in out["rootHtml"]
    assert "Bearer" not in out["rootHtml"]


def test_spaces_ui_widget_manager_shows_inline_agent_bridge_status(driver_path):
    out = _run_spaces_scenario(driver_path, "list")

    assert "Agent bridge: 2 queued" in out["rootHtml"]
    assert "Latest: widget.refresh · queued" in out["rootHtml"]
    assert "Event: evt-refresh" in out["rootHtml"]
    assert "Use token" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]
    assert "Bearer" not in out["rootHtml"]


def test_spaces_ui_save_widget_posts_to_upsert_and_refreshes_widgets(driver_path):
    out = _run_spaces_scenario(driver_path, "save")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/upsert")

    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_id": "lab",
        "widget": {"id": "notes", "title": "Notes", "kind": "markdown", "layout": {"x": 2, "y": 3, "w": 8, "h": 5}},
    }
    assert not any(call["path"] == "api/spaces/widget/patch" for call in out["calls"])
    assert out["calls"][-1]["path"] == "api/spaces/widgets?space_id=lab"


def test_spaces_ui_edit_widget_uses_patch_route_and_preserves_source_bodies(driver_path):
    out = _run_spaces_scenario(driver_path, "editWidgetSave")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/patch")

    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_id": "lab",
        "widget_id": "weather",
        "patch": {
            "title": "Weather patched",
            "kind": "markdown",
            "layout": {"x": 4, "y": 5, "w": 9, "h": 6},
        },
    }
    assert not any(call["path"] == "api/spaces/widget/upsert" for call in out["calls"])
    assert out["calls"][-1]["path"] == "api/spaces/widgets?space_id=lab"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]


def test_spaces_ui_move_widget_posts_metadata_only_layout_patch(driver_path):
    out = _run_spaces_scenario(driver_path, "moveWidgetLeft")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/patch")

    assert "Move left" in out["beforeHtml"]
    assert "Move right" in out["beforeHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_id": "lab",
        "widget_id": "weather",
        "patch": {"layout": {"x": 11, "y": 3, "w": 5, "h": 4}},
    }
    assert not any(call["path"] == "api/spaces/widget/upsert" for call in out["calls"])
    assert out["calls"][-1]["path"] == "api/spaces/widgets?space_id=lab"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_resize_widget_posts_metadata_only_layout_patch(driver_path):
    out = _run_spaces_scenario(driver_path, "resizeWidgetWider")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/patch")

    assert "Wider" in out["beforeHtml"]
    assert "Narrower" in out["beforeHtml"]
    assert "Taller" in out["beforeHtml"]
    assert "Shorter" in out["beforeHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_id": "lab",
        "widget_id": "weather",
        "patch": {"layout": {"x": 12, "y": 3, "w": 6, "h": 4}},
    }
    assert not any(call["path"] == "api/spaces/widget/upsert" for call in out["calls"])
    assert out["calls"][-1]["path"] == "api/spaces/widgets?space_id=lab"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_minimize_widget_posts_metadata_only_layout_patch(driver_path):
    out = _run_spaces_scenario(driver_path, "minimizeWidget")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/patch")

    assert "Minimize" in out["beforeHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_id": "lab",
        "widget_id": "weather",
        "patch": {"layout": {"x": 12, "y": 3, "w": 5, "h": 4, "minimized": True}},
    }
    assert not any(call["path"] == "api/spaces/widget/upsert" for call in out["calls"])
    assert out["calls"][-1]["path"] == "api/spaces/widgets?space_id=lab"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_restore_widget_posts_metadata_only_layout_patch(driver_path):
    out = _run_spaces_scenario(driver_path, "restoreWidget")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/patch")

    assert "Restore" in out["beforeHtml"]
    assert "minimized" in out["beforeHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_id": "lab",
        "widget_id": "weather",
        "patch": {"layout": {"x": 12, "y": 3, "w": 5, "h": 4, "minimized": False}},
    }
    assert not any(call["path"] == "api/spaces/widget/upsert" for call in out["calls"])
    assert out["calls"][-1]["path"] == "api/spaces/widgets?space_id=lab"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_delete_widget_fails_closed_without_shared_confirm(driver_path):
    out = _run_spaces_scenario(driver_path, "delete")

    assert not any(call["path"] == "api/spaces/widget/delete" for call in out["calls"])
    assert out["dialogs"] == []


def test_spaces_ui_delete_widget_cancel_does_not_send_delete(driver_path):
    out = _run_spaces_scenario(driver_path, "deleteWidgetCancelled")

    assert out["dialogs"]
    assert out["dialogs"][0]["title"] == "Delete widget?"
    assert not any(call["path"] == "api/spaces/widget/delete" for call in out["calls"])


def test_spaces_ui_delete_widget_confirm_posts_to_delete_and_refreshes_widgets(driver_path):
    out = _run_spaces_scenario(driver_path, "deleteWidgetConfirmed")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/delete")

    assert out["dialogs"]
    assert out["dialogs"][0]["title"] == "Delete widget?"
    assert out["dialogs"][0]["confirmLabel"] == "Delete widget"
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "lab", "widget_id": "weather"}
    assert out["calls"][-1]["path"] == "api/spaces/widgets?space_id=lab"


def test_spaces_ui_edit_widget_prefills_safe_metadata_form_without_fetching_renderer(driver_path):
    out = _run_spaces_scenario(driver_path, "editWidget")

    assert out["values"]["#capyWidgetId"] == "weather"
    assert out["values"]["#capyWidgetTitle"] == "<Weather>"
    assert out["values"]["#capyWidgetKind"] == "markdown"
    assert out["values"]["#capyWidgetX"] == "12"
    assert out["values"]["#capyWidgetY"] == "3"
    assert out["values"]["#capyWidgetW"] == "5"
    assert out["values"]["#capyWidgetH"] == "4"
    assert not any(call["path"] == "api/spaces/widget?space_id=lab&widget_id=weather" for call in out["calls"])
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]


def test_spaces_ui_view_widget_details_fetches_and_renders_safe_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "viewWidgetDetails")

    assert "View details" in out["beforeHtml"]
    assert {"path": "api/spaces/widget?space_id=lab&widget_id=weather", "method": "GET", "body": ""} in out["calls"]
    runtime_call = next(call for call in out["calls"] if call["path"] == "api/spaces/tool" and "runtime_contract" in call["body"])
    assert json.loads(runtime_call["body"]) == {
        "action": "space.widget.runtime_contract",
        "space_id": "lab",
        "widget_id": "weather",
    }
    assert "Widget details" in out["rootHtml"]
    assert "Back to widgets" in out["rootHtml"]
    assert "data-capy-action=\"loadWidgets\"" in out["rootHtml"]
    assert "&lt;Weather&gt;" in out["rootHtml"]
    assert "markdown" in out["rootHtml"]
    assert "x12 y3 · 5×4" in out["rootHtml"]
    assert "content_status: agent-managed-empty" in out["rootHtml"]
    assert "weather: location, country, units, status, current" in out["rootHtml"]
    assert "location: Prague" in out["rootHtml"]
    assert "current: condition, temperature_c, feels_like_c" in out["rootHtml"]
    assert "temperature_c: 18" in out["rootHtml"]
    assert "export: pdf" in out["rootHtml"]
    assert "interaction: refresh" in out["rootHtml"]
    assert "permissions: network" in out["rootHtml"]
    assert "event_bridge: event_name, status" in out["rootHtml"]
    assert "Suggested prompt" in out["rootHtml"]
    assert "Ask Capy to refresh or explain the Prague weather widget" in out["rootHtml"]
    assert "Suggested event: widget.refresh" in out["rootHtml"]
    assert "Runtime contract: sandbox-contract-draft" in out["rootHtml"]
    assert "Execution: generated-code-disabled" in out["rootHtml"]
    assert "Allowed messages: capy:ready, capy:resize, capy:agent:prompt" in out["rootHtml"]
    assert "Blocked messages: capy:raw:eval, capy:data:put, capy:data:get, capy:asset:url" in out["rootHtml"]
    assert "Network policy: deny · schemes: https · agent-mediated" in out["rootHtml"]
    assert "Approval required: external-navigation, network-fetch, generated-code-enable" in out["rootHtml"]
    assert "credential" not in out["rootHtml"].lower()
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "onerror" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_sandbox_postmessage_agent_prompt_requires_approval_and_queues_metadata_only_event(driver_path):
    out = _run_spaces_scenario(driver_path, "runtimePromptMessage")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/event")
    body = json.loads(post["body"])
    dialog_blob = json.dumps(out["dialogs"])

    assert "Sandbox event bridge" in out["beforeHtml"]
    assert "data-runtime-token=" in out["beforeHtml"]
    assert out["dialogs"]
    assert body == {
        "space_id": "lab",
        "widget_id": "weather",
        "event_name": "agent.prompt",
        "prompt": "Refresh safely without [REDACTED] or bad()",
        "payload": {"source": "sandbox-postmessage", "message_type": "capy:agent:prompt"},
    }
    assert "Widget event queued" in out["rootHtml"]
    assert "Sandbox prompt queued" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]
    assert "SECRET" not in dialog_blob
    assert "<script" not in dialog_blob.lower()


def test_spaces_ui_sandbox_postmessage_blocks_raw_eval_without_network_call(driver_path):
    out = _run_spaces_scenario(driver_path, "runtimeBlockedMessage")

    assert "Sandbox event bridge" in out["beforeHtml"]
    assert "Sandbox message blocked" in out["rootHtml"]
    assert "Sandbox message blocked: capy:raw:eval" not in out["rootHtml"]
    assert not any(call["path"] == "api/spaces/widget/event" for call in out["calls"])
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_sandbox_postmessage_rejects_conflicting_message_type_aliases(driver_path):
    out = _run_spaces_scenario(driver_path, "runtimeConflictingMessageType")

    assert "Sandbox event bridge" in out["beforeHtml"]
    assert out["dialogs"] == []
    assert not any(call["path"] == "api/spaces/widget/event" for call in out["calls"])
    assert "Sandbox message blocked" in out["rootHtml"]
    assert "Sandbox prompt queued" not in out["rootHtml"]
    assert "Sandbox message blocked: capy:raw:eval" not in out["rootHtml"]
    assert "eval(" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_sandbox_postmessage_cancelled_prompt_does_not_queue_event(driver_path):
    out = _run_spaces_scenario(driver_path, "runtimePromptCancelled")

    assert "Sandbox event bridge" in out["beforeHtml"]
    assert out["dialogs"]
    assert not any(call["path"] == "api/spaces/widget/event" for call in out["calls"])
    assert "Sandbox prompt queued" not in out["rootHtml"]


def test_spaces_ui_sandbox_postmessage_rejects_foreign_origin_even_with_valid_token(driver_path):
    out = _run_spaces_scenario(driver_path, "runtimePromptForeignOrigin")

    assert "Sandbox event bridge" in out["beforeHtml"]
    assert out["dialogs"] == []
    assert not any(call["path"] == "api/spaces/widget/event" for call in out["calls"])
    assert "Sandbox prompt queued" not in out["rootHtml"]


def test_spaces_ui_sandbox_postmessage_ignores_stale_runtime_shell(driver_path):
    out = _run_spaces_scenario(driver_path, "runtimePromptStaleShell")

    assert "Sandbox event bridge" in out["beforeHtml"]
    assert out["dialogs"] == []
    assert not any(call["path"] == "api/spaces/widget/event" for call in out["calls"])
    assert "Sandbox prompt queued" not in out["rootHtml"]


def test_spaces_ui_sandbox_postmessage_redacts_broad_sensitive_prompt_markers(driver_path):
    out = _run_spaces_scenario(driver_path, "runtimeSensitivePromptMessage")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/event")
    body = json.loads(post["body"])
    dialog_blob = json.dumps(out["dialogs"])
    combined = body["prompt"] + " " + dialog_blob + " " + out["rootHtml"]

    assert "[REDACTED]" in body["prompt"]
    assert "access_token" not in combined.lower()
    assert "api_auth" not in combined.lower()
    assert "cookie" not in combined.lower()
    assert "credential" not in combined.lower()
    assert "source=" not in combined.lower()
    assert "html=" not in combined.lower()
    assert "javascript:" not in combined.lower()
    assert "onerror" not in combined.lower()
    assert "SECRET" not in combined
    assert "<script" not in combined.lower()


def test_spaces_ui_sandbox_postmessage_redacts_code_like_sensitive_prompt_values(driver_path):
    out = _run_spaces_scenario(driver_path, "runtimeCodeLikeSensitivePrompt")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/event")
    body = json.loads(post["body"])
    dialog_blob = json.dumps(out["dialogs"])
    combined = body["prompt"] + " " + dialog_blob + " " + out["rootHtml"]

    assert body["prompt"] == "[REDACTED] sandbox prompt: unsafe markers omitted"
    assert "function bad" not in combined.lower()
    assert "api_auth" not in combined.lower()
    assert "bearer" not in combined.lower()
    assert "cookie" not in combined.lower()
    assert "session abc" not in combined.lower()
    assert "source =" not in combined.lower()
    assert "data =" not in combined.lower()


def test_spaces_ui_sandbox_postmessage_redacts_auth_prompt_generated_code_and_secret_shapes(driver_path):
    out = _run_spaces_scenario(driver_path, "runtimeGeneratedAuthPromptMarkers")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/event")
    body = json.loads(post["body"])
    dialog_blob = json.dumps(out["dialogs"])
    combined = body["prompt"] + " " + dialog_blob + " " + out["rootHtml"]
    prompt_and_dialog = body["prompt"] + " " + dialog_blob

    assert body["prompt"] == "[REDACTED] sandbox prompt: unsafe markers omitted"
    assert "auth =" not in combined.lower()
    assert "prompt =" not in combined.lower()
    assert "generated-code:" not in prompt_and_dialog.lower()
    assert "sk-testsecretlooking" not in prompt_and_dialog.lower()
    assert "function render" not in prompt_and_dialog.lower()


def test_spaces_ui_sandbox_postmessage_blocks_data_and_raw_eval_mutations_without_network_call(driver_path):
    out = _run_spaces_scenario(driver_path, "runtimeBlockedMutationMessages")
    root_html = out["rootHtml"]
    root_lower = root_html.lower()

    assert "Sandbox event bridge" in out["beforeHtml"]
    assert root_html.count("Sandbox message blocked") >= 3
    assert root_html.count("Blocked by Capy runtime contract; no widget event was queued.") >= 3
    assert not any(call["path"] == "api/spaces/widget/event" for call in out["calls"])
    assert "Sandbox message blocked: capy:data:put" not in root_html
    assert "Sandbox message blocked: capy:eval:run" not in root_html
    assert "Sandbox message blocked: capy:raw:source" not in root_html
    assert "session abc" not in root_lower
    assert "SECRET" not in root_html
    assert "<script>" not in root_html
    assert "renderer" not in root_lower
    assert "source" not in root_lower


def test_spaces_ui_sandbox_postmessage_blocks_data_get_and_asset_url_reads_without_backend_or_event(driver_path):
    out = _run_spaces_scenario(driver_path, "runtimeBlockedReadMessages")
    root_html = out["rootHtml"]
    root_lower = root_html.lower()

    assert "Sandbox event bridge" in out["beforeHtml"]
    assert out["dialogs"] == []
    assert root_html.count("Sandbox message blocked") >= 2
    assert root_html.count("Blocked by Capy runtime contract; no widget event was queued.") >= 2
    assert not any(call["path"] == "api/spaces/widget/event" for call in out["calls"])
    assert not any("api/spaces/asset" in call["path"].lower() for call in out["calls"])
    assert not any("api/spaces/data" in call["path"].lower() for call in out["calls"])
    assert "Sandbox message blocked: capy:data:get" not in root_html
    assert "Sandbox message blocked: capy:asset:url" not in root_html
    assert "Sandbox prompt queued" not in root_html
    assert "Widget event queued" not in root_html
    assert "shared_data.private_weather_token" not in root_lower
    assert "spaces/lab/widgets/weather/data.json" not in root_lower
    assert "weather-map" not in root_lower
    assert "asset.example" not in root_lower
    assert "session abc" not in root_lower
    assert "bearer" not in root_lower
    assert "authorization" not in root_lower
    assert "cookie" not in root_lower
    assert "api_key" not in root_lower
    assert "SECRET" not in root_html
    assert "SECRET_SOURCE" not in root_html
    assert "<script>" not in root_html
    assert "onerror" not in root_lower
    assert "renderer" not in root_lower
    assert "source" not in root_lower


def test_spaces_ui_sandbox_postmessage_blocked_status_does_not_reflect_hostile_type(driver_path):
    out = _run_spaces_scenario(driver_path, "runtimeBlockedHostileTypeReflection")
    root_html = out["rootHtml"]
    root_lower = root_html.lower()

    assert "Sandbox event bridge" in out["beforeHtml"]
    assert out["dialogs"] == []
    assert "Sandbox message blocked" in root_html
    assert "Blocked by Capy runtime contract; no widget event was queued." in root_html
    assert not any(call["path"] == "api/spaces/widget/event" for call in out["calls"])
    assert not any("api/spaces/asset" in call["path"].lower() for call in out["calls"])
    assert not any("api/spaces/data" in call["path"].lower() for call in out["calls"])
    assert "capy:raw:SECRET_VALUE_DO_NOT_LEAK" not in root_html
    assert "SECRET" not in root_html
    assert "SECRET_SOURCE" not in root_html
    assert "<script>" not in root_html
    assert "renderer" not in root_lower
    assert "source" not in root_lower
    assert "api_key" not in root_lower


def test_spaces_ui_sandbox_runtime_tokens_rotate_and_invalidate_old_token(driver_path):
    out = _run_spaces_scenario(driver_path, "runtimeTokenRotates")

    assert out["rootDataset"]["firstRuntimeToken"]
    assert out["rootDataset"]["secondRuntimeToken"]
    assert out["rootDataset"]["firstRuntimeToken"] != out["rootDataset"]["secondRuntimeToken"]
    assert out["dialogs"] == []
    assert not any(call["path"] == "api/spaces/widget/event" for call in out["calls"])


def test_spaces_ui_widget_detail_shows_visible_weather_observation_without_generated_body(driver_path):
    out = _run_spaces_scenario(driver_path, "viewWidgetDetails")

    assert "Current weather observation" in out["rootHtml"]
    assert "Prague, CZ" in out["rootHtml"]
    assert "18 °C" in out["rootHtml"]
    assert "Feels like 17 °C" in out["rootHtml"]
    assert "partly cloudy" in out["rootHtml"]
    assert "Observation status: observation-ready" in out["rootHtml"]
    assert "Partly cloudy in Prague; refreshed through agent-mediated weather metadata." in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_widget_detail_can_request_pdf_export_as_metadata_event(driver_path):
    out = _run_spaces_scenario(driver_path, "requestWidgetPdfExport")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/event")

    assert "Request PDF export" in out["beforeHtml"]
    assert out["dialogs"] == []
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_id": "lab",
        "widget_id": "weather",
        "event_name": "widget.export.pdf",
        "payload": {"source": "widget-detail", "action": "export_pdf", "widget_title": "<Weather>"},
    }
    assert out["calls"][-1]["path"] == "api/spaces/widgets?space_id=lab"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_notes_widget_detail_saves_real_editable_notes_via_patch(driver_path):
    out = _run_spaces_scenario(driver_path, "saveNotesWidget")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/patch")

    assert "Editable notes" in out["beforeHtml"]
    assert "Initial notes body" in out["beforeHtml"]
    assert "Save notes" in out["beforeHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_id": "lab",
        "widget_id": "notes-main",
        "patch": {
            "notes": {
                "body": "Updated real note\n\n- persists through widget patch",
                "format": "markdown",
                "updated_from": "spaces-ui",
            }
        },
    }
    assert out["calls"][-1]["path"] == "api/spaces/widget?space_id=lab&widget_id=notes-main"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_ask_widget_uses_shared_prompt_and_queues_agent_event(driver_path):
    out = _run_spaces_scenario(driver_path, "askWidget")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/event")

    assert out["dialogs"]
    assert out["dialogs"][0]["title"] == "Ask Capy about this widget"
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_id": "lab",
        "widget_id": "weather",
        "event_name": "agent.prompt",
        "prompt": "Refresh the weather widget",
        "payload": {"source": "widget-manager", "widget_title": "<Weather>"},
    }
    assert out["calls"][-1]["path"] == "api/spaces/widgets?space_id=lab"
    assert "Weather prompt queued" in out["rootHtml"]
    assert "weather · agent.prompt · evt1" in out["rootHtml"]
    assert "Refresh the weather widget" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_ask_widget_fails_closed_without_shared_prompt(driver_path):
    out = _run_spaces_scenario(driver_path, "askWidgetNoPrompt")

    assert not any(call["path"] == "api/spaces/widget/event" for call in out["calls"])


def test_spaces_ui_refresh_widget_queues_metadata_only_refresh_event(driver_path):
    out = _run_spaces_scenario(driver_path, "refreshWidget")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/event")

    assert "Refresh" in out["beforeHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_id": "lab",
        "widget_id": "weather",
        "event_name": "widget.refresh",
        "payload": {"source": "widget-manager", "action": "refresh"},
    }
    assert out["dialogs"] == []
    assert out["calls"][-1]["path"] == "api/spaces/widgets?space_id=lab"
    assert "Weather refresh queued" in out["rootHtml"]
    assert "weather · widget.refresh · evt1" in out["rootHtml"]
    assert "Agent bridge: 2 queued" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_create_space_posts_to_create_and_refreshes_spaces(driver_path):
    out = _run_spaces_scenario(driver_path, "createSpace")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/create")

    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_id": "ops",
        "name": "Ops",
        "description": "<b>Operations</b>",
    }
    assert out["calls"][-1]["path"] == "api/spaces"


def test_spaces_ui_create_space_from_chat_posts_current_session_and_syncs_active_space(driver_path):
    out = _run_spaces_scenario(driver_path, "createSpaceFromChat")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/create-from-session")

    assert "Create from current chat" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"session_id": "session-123"}
    assert out["capySpaceSyncs"] == ["research-chat-space"]
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_weather_demo_posts_template_and_shows_safe_open_manage_status(driver_path):
    out = _run_spaces_scenario(driver_path, "installWeatherDemo")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install weather demo" in out["rootHtml"]
    assert "Weather demo installed" in out["rootHtml"]
    assert "Weather Demo" in out["rootHtml"]
    assert "1 widget" in out["rootHtml"]
    assert "Open weather demo" in out["rootHtml"]
    assert "Manage weather widget" in out["rootHtml"]
    assert "Run weather smoke" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="weather-demo"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="weather-demo"' in out["rootHtml"]
    assert 'data-capy-action="runDemoSmoke" data-demo="demo_weather_widget"' in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "weather"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_research_harness_posts_template_and_shows_safe_open_manage_status(driver_path):
    out = _run_spaces_scenario(driver_path, "installResearchHarness")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install research harness" in out["rootHtml"]
    assert "Research harness installed" in out["rootHtml"]
    assert "Research Harness" in out["rootHtml"]
    assert "2 widgets" in out["rootHtml"]
    assert "Open research harness" in out["rootHtml"]
    assert "Manage research widgets" in out["rootHtml"]
    assert "Run research smoke" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="research-harness"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="research-harness"' in out["rootHtml"]
    assert 'data-capy-action="runDemoSmoke" data-demo="demo_research_harness_pdf_export"' in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "research"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_dashboard_demo_posts_template_and_shows_safe_open_manage_status(driver_path):
    out = _run_spaces_scenario(driver_path, "installDashboardDemo")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install dashboard demo" in out["rootHtml"]
    assert "Dashboard demo installed" in out["rootHtml"]
    assert "Daily Dashboard" in out["rootHtml"]
    assert "2 widgets" in out["rootHtml"]
    assert "Open dashboard demo" in out["rootHtml"]
    assert "Manage dashboard widgets" in out["rootHtml"]
    assert "Run dashboard smoke" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="daily-dashboard"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="daily-dashboard"' in out["rootHtml"]
    assert 'data-capy-action="runDemoSmoke" data-demo="demo_daily_dashboard"' in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "dashboard"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_kanban_board_posts_template_and_shows_safe_open_manage_status(driver_path):
    out = _run_spaces_scenario(driver_path, "installKanbanBoard")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install kanban board" in out["rootHtml"]
    assert "Kanban board installed" in out["rootHtml"]
    assert "Kanban Board" in out["rootHtml"]
    assert "2 widgets" in out["rootHtml"]
    assert "Open kanban board" in out["rootHtml"]
    assert "Manage kanban widgets" in out["rootHtml"]
    assert "Run kanban smoke" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="kanban-board"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="kanban-board"' in out["rootHtml"]
    assert 'data-capy-action="runDemoSmoke" data-demo="demo_kanban_board"' in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "kanban"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_notes_app_posts_template_and_shows_safe_open_manage_status(driver_path):
    out = _run_spaces_scenario(driver_path, "installNotesApp")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install notes app" in out["rootHtml"]
    assert "Notes app installed" in out["rootHtml"]
    assert "Notes App" in out["rootHtml"]
    assert "Open notes app" in out["rootHtml"]
    assert "Manage notes widgets" in out["rootHtml"]
    assert "Run notes smoke" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="notes-app"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="notes-app"' in out["rootHtml"]
    assert 'data-capy-action="runDemoSmoke" data-demo="demo_notes_app"' in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "notes"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_browser_surface_posts_template_and_shows_safe_open_manage_status(driver_path):
    out = _run_spaces_scenario(driver_path, "installBrowserSurface")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install browser surface" in out["rootHtml"]
    assert "Browser surface installed" in out["rootHtml"]
    assert "Browser Surface" in out["rootHtml"]
    assert "2 widgets" in out["rootHtml"]
    assert "Open browser surface" in out["rootHtml"]
    assert "Manage browser widgets" in out["rootHtml"]
    assert "Run browser smoke" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="browser-surface"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="browser-surface"' in out["rootHtml"]
    assert 'data-capy-action="runDemoSmoke" data-demo="demo_browser_cocontrol_google_or_test_site"' in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "browser"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_stock_chart_posts_template_and_shows_safe_open_manage_status(driver_path):
    out = _run_spaces_scenario(driver_path, "installStockChart")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install stock chart" in out["rootHtml"]
    assert "Stock chart installed" in out["rootHtml"]
    assert "Stock Chart" in out["rootHtml"]
    assert "3 widgets" in out["rootHtml"]
    assert "Open stock chart" in out["rootHtml"]
    assert "Manage stock widgets" in out["rootHtml"]
    assert "Run stock smoke" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="stock-chart"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="stock-chart"' in out["rootHtml"]
    assert 'data-capy-action="runDemoSmoke" data-demo="demo_stock_chart"' in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "stock"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_camera_dashboard_posts_template_and_shows_safe_open_manage_status(driver_path):
    out = _run_spaces_scenario(driver_path, "installCameraDashboard")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install camera dashboard" in out["rootHtml"]
    assert "Camera dashboard installed" in out["rootHtml"]
    assert "Camera Dashboard" in out["rootHtml"]
    assert "3 widgets" in out["rootHtml"]
    assert "Open camera dashboard" in out["rootHtml"]
    assert "Manage camera widgets" in out["rootHtml"]
    assert "Run camera smoke" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="camera-dashboard"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="camera-dashboard"' in out["rootHtml"]
    assert 'data-capy-action="runDemoSmoke" data-demo="demo_camera_dashboard"' in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "camera"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_big_bang_onboarding_posts_template_and_refreshes_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "installBigBangOnboarding")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install Big Bang onboarding" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "big-bang"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_game_sandbox_posts_template_and_shows_safe_open_manage_status(driver_path):
    out = _run_spaces_scenario(driver_path, "installGameSandbox")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install game sandbox" in out["rootHtml"]
    assert "Game sandbox installed" in out["rootHtml"]
    assert "Game Sandbox" in out["rootHtml"]
    assert "3 widgets" in out["rootHtml"]
    assert "Open game sandbox" in out["rootHtml"]
    assert "Manage game widgets" in out["rootHtml"]
    assert "Run snake smoke" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="game-sandbox"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="game-sandbox"' in out["rootHtml"]
    assert 'data-capy-action="runDemoSmoke" data-demo="demo_snake_iterative_repair"' in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "game"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_music_sequencer_posts_template_and_shows_safe_open_manage_status(driver_path):
    out = _run_spaces_scenario(driver_path, "installMusicSequencer")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install music sequencer" in out["rootHtml"]
    assert "Music sequencer installed" in out["rootHtml"]
    assert "Music Sequencer" in out["rootHtml"]
    assert "4 widgets" in out["rootHtml"]
    assert "Open music sequencer" in out["rootHtml"]
    assert "Manage music widgets" in out["rootHtml"]
    assert "Run music smoke" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="music-sequencer"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="music-sequencer"' in out["rootHtml"]
    assert 'data-capy-action="runDemoSmoke" data-demo="demo_step_sequencer_piano_roll"' in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "music"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_local_service_dashboard_posts_template_and_shows_safe_open_manage_status(driver_path):
    out = _run_spaces_scenario(driver_path, "installLocalServiceDashboard")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install local service dashboard" in out["rootHtml"]
    assert "Local service dashboard installed" in out["rootHtml"]
    assert "Local Service Dashboard" in out["rootHtml"]
    assert "4 widgets" in out["rootHtml"]
    assert "Open local service dashboard" in out["rootHtml"]
    assert "Manage service widgets" in out["rootHtml"]
    assert "Run local service smoke" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="local-service-dashboard"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="local-service-dashboard"' in out["rootHtml"]
    assert 'data-capy-action="runDemoSmoke" data-demo="demo_local_agent_control_dashboard"' in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "service"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_model_setup_posts_template_and_shows_safe_open_manage_status(driver_path):
    out = _run_spaces_scenario(driver_path, "installModelSetup")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install model setup" in out["rootHtml"]
    assert "Model setup installed" in out["rootHtml"]
    assert "Model Provider Setup" in out["rootHtml"]
    assert "4 widgets" in out["rootHtml"]
    assert "Open model setup" in out["rootHtml"]
    assert "Manage provider widgets" in out["rootHtml"]
    assert "Run provider setup smoke" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="model-provider-setup"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="model-provider-setup"' in out["rootHtml"]
    assert 'data-capy-action="runDemoSmoke" data-demo="demo_provider_setup"' in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "model-setup"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "token" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_runs_demo_parity_smoke_from_safe_catalog(driver_path):
    out = _run_spaces_scenario(driver_path, "runDemoParitySmoke")
    list_get = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/runs")
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")

    assert list_get["method"] == "GET"
    assert "Demo parity smoke runner" in out["beforeHtml"]
    assert "Weather answer → persistent widget" in out["beforeHtml"]
    assert "Time travel rollback" in out["beforeHtml"]
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_weather_widget"}
    assert not any(
        call["path"] == "api/spaces/tool" and json.loads(call["body"] or "{}").get("action", "").startswith("space.demo")
        for call in out["calls"]
    )
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "demo_weather_widget" in out["rootHtml"]
    assert "Weather Demo Smoke" in out["rootHtml"]
    assert "Widgets: 1" in out["rootHtml"]
    assert "Persistence: checked" in out["rootHtml"]
    assert "Rollback point: yes" in out["rootHtml"]
    assert "Open demo Space" in out["rootHtml"]
    assert "Manage weather widget" in out["rootHtml"]
    assert "data-space-id=\"demo-weather-widget\"" in out["rootHtml"]
    assert "Current weather observation" in out["rootHtml"]
    assert "Prague, CZ" in out["rootHtml"]
    assert "18 °C" in out["rootHtml"]
    assert "partly cloudy" in out["rootHtml"]
    assert "Observation status: observation-ready" in out["rootHtml"]
    assert "Partly cloudy in Prague; refreshed through agent-mediated weather metadata." in out["rootHtml"]
    assert "Prompt → widget flow" in out["rootHtml"]
    assert "Weather demo checklist" in out["rootHtml"]
    assert "<li>Chat answer recorded</li>" in out["rootHtml"]
    assert "<li>Widget created from request</li>" in out["rootHtml"]
    assert "<li>Persistent widget verified after reload</li>" in out["rootHtml"]
    assert "<li>1. Chat answer recorded</li>" not in out["rootHtml"]
    assert "<li>2. Widget created from request</li>" not in out["rootHtml"]
    assert "<li>3. Persistent widget verified after reload</li>" not in out["rootHtml"]
    assert "Blank space: yes" in out["rootHtml"]
    assert "What is the weather in Prague?" in out["rootHtml"]
    assert "Chat answer: recorded" in out["rootHtml"]
    assert "Answer preview: Prague is partly cloudy at 18 °C; the answer is now saved as safe widget metadata." in out["rootHtml"]
    assert "Widget request: show it to me in a widget" in out["rootHtml"]
    assert "Widget after reload: verified" in out["rootHtml"]
    assert "Network mode: agent-mediated" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_weather_walkthrough_is_visible_and_runs_prompt_to_widget_flow(driver_path):
    out = _run_spaces_scenario(driver_path, "runWeatherWalkthrough")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")

    assert "Run weather walkthrough" in out["beforeHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"demo": "demo_weather_widget"}
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "Prompt → widget flow" in out["rootHtml"]
    assert "Weather demo checklist" in out["rootHtml"]
    assert "Current weather observation" in out["rootHtml"]
    assert "Manage weather widget" in out["rootHtml"]
    assert {"path": "api/spaces/widget/events?space_id=demo-weather-widget", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-weather-widget", "method": "GET", "body": ""} in out["calls"]
    assert "Widgets for demo-weather-widget" in out["rootHtml"]
    assert "Weather in Prague" in out["rootHtml"]
    assert "weather-current" in out["rootHtml"]
    assert "Agent bridge: 2 queued" in out["rootHtml"]
    assert "Latest: widget.refresh · queued" in out["rootHtml"]
    assert "Queued widget events" in out["rootHtml"]
    assert "note: [REDACTED]" in out["rootHtml"]
    assert "prompt: [REDACTED]" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_notes_walkthrough_is_visible_and_opens_widget_manager_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runNotesWalkthrough")

    assert "Run notes walkthrough" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_notes_app"}
    assert {"path": "api/spaces/widget/events?space_id=demo-notes-app", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-notes-app", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "Notes app checklist" in out["rootHtml"]
    assert "Saved notes preview" in out["rootHtml"]
    assert "Widgets for demo-notes-app" in out["rootHtml"]
    assert "notes-folders" in out["rootHtml"]
    assert "notes-editor" in out["rootHtml"]
    assert "notes-preview" in out["rootHtml"]
    assert "notes-attachments" in out["rootHtml"]
    assert "Queued widget events" in out["rootHtml"]
    assert "notes.save" in out["rootHtml"]
    assert "note: [REDACTED]" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_kanban_walkthrough_is_visible_and_opens_widget_manager_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runKanbanWalkthrough")

    assert "Run kanban walkthrough" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_kanban_board"}
    assert {"path": "api/spaces/widget/events?space_id=demo-kanban-board", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-kanban-board", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "Kanban board preview" in out["rootHtml"]
    assert "Widgets for demo-kanban-board" in out["rootHtml"]
    assert "kanban-backlog" in out["rootHtml"]
    assert "kanban-doing" in out["rootHtml"]
    assert "kanban-done" in out["rootHtml"]
    assert "Queued widget events" in out["rootHtml"]
    assert "kanban.card.move" in out["rootHtml"]
    assert "card: [REDACTED]" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_snake_walkthrough_is_visible_and_opens_repair_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runSnakeWalkthrough")

    assert "Run snake repair walkthrough" in out["beforeHtml"]
    assert "Snake repair loop" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_snake_iterative_repair"}
    assert {"path": "api/spaces/widget/events?space_id=demo-snake-iterative-repair", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-snake-iterative-repair", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "Snake Repair Smoke" in out["rootHtml"]
    assert "Action: snake-repair-queued" in out["rootHtml"]
    assert "Queued events: 1" in out["rootHtml"]
    assert "Snake repair preview" in out["rootHtml"]
    assert "Game: snake" in out["rootHtml"]
    assert "First attempt: broken-placeholder" in out["rootHtml"]
    assert "Renderer status: generated-code-disabled" in out["rootHtml"]
    assert "Focus policy: explicit-click" in out["rootHtml"]
    assert "Widgets for demo-snake-iterative-repair" in out["rootHtml"]
    assert "game-canvas" in out["rootHtml"]
    assert "game-controls" in out["rootHtml"]
    assert "game-repair-notes" in out["rootHtml"]
    assert "agent.repair" in out["rootHtml"]
    assert "action: repair-snake" in out["rootHtml"]
    assert "authorization" not in out["rootHtml"].lower()
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_dashboard_walkthrough_is_visible_and_opens_widget_manager_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runDashboardWalkthrough")

    assert "Run dashboard walkthrough" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_daily_dashboard"}
    assert {"path": "api/spaces/widget/events?space_id=demo-daily-dashboard", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-daily-dashboard", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "Daily Dashboard Smoke" in out["rootHtml"]
    assert "Action: daily-dashboard-seeded" in out["rootHtml"]
    assert "Widgets for demo-daily-dashboard" in out["rootHtml"]
    assert "dashboard-prices" in out["rootHtml"]
    assert "dashboard-news" in out["rootHtml"]
    assert "dashboard-agenda" in out["rootHtml"]
    assert "dashboard-brief" in out["rootHtml"]
    assert "Queued widget events" in out["rootHtml"]
    assert "dashboard.refresh" in out["rootHtml"]
    assert "action: refresh-dashboard" in out["rootHtml"]
    assert "authorization" not in out["rootHtml"].lower()
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_camera_walkthrough_is_visible_and_opens_widget_manager_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runCameraWalkthrough")

    assert "Run camera walkthrough" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_camera_dashboard"}
    assert {"path": "api/spaces/widget/events?space_id=demo-camera-dashboard", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-camera-dashboard", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "Camera Dashboard Smoke" in out["rootHtml"]
    assert "Action: camera-dashboard-seeded" in out["rootHtml"]
    assert "Widgets for demo-camera-dashboard" in out["rootHtml"]
    assert "camera-grid" in out["rootHtml"]
    assert "camera-permissions" in out["rootHtml"]
    assert "camera-incidents" in out["rootHtml"]
    assert "authorization" not in out["rootHtml"].lower()
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_stock_walkthrough_is_visible_and_opens_market_snapshot_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runStockWalkthrough")

    assert "Run stock walkthrough" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_stock_chart"}
    assert {"path": "api/spaces/widget/events?space_id=demo-stock-chart", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-stock-chart", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "Stock Chart Smoke" in out["rootHtml"]
    assert "Action: stock-snapshot-recorded" in out["rootHtml"]
    assert "Stock chart preview" in out["rootHtml"]
    assert "NVDA · 905.10 · +1.8% · GPU demand watch" in out["rootHtml"]
    assert "AAPL · 182.40 · -0.3% · services margin watch" in out["rootHtml"]
    assert "GOOGL · 171.25 · +0.6% · AI search watch" in out["rootHtml"]
    assert "Network mode: agent-mediated" in out["rootHtml"]
    assert "Manage stock widgets" in out["rootHtml"]
    assert "Widgets for demo-stock-chart" in out["rootHtml"]
    assert "stock-chart" in out["rootHtml"]
    assert "stock-watchlist" in out["rootHtml"]
    assert "stock-notes" in out["rootHtml"]
    assert "Queued widget events" in out["rootHtml"]
    assert "stock.refresh" in out["rootHtml"]
    assert "action: refresh-market-snapshot" in out["rootHtml"]
    assert "authorization" not in out["rootHtml"].lower()
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_local_service_walkthrough_is_visible_and_opens_widget_manager_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runLocalServiceWalkthrough")

    assert "Run local service walkthrough" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_local_agent_control_dashboard"}
    assert {"path": "api/spaces/widget/events?space_id=demo-local-agent-control-dashboard", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-local-agent-control-dashboard", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "Local Service Dashboard Smoke" in out["rootHtml"]
    assert "Action: local-service-dashboard-seeded" in out["rootHtml"]
    assert "Widgets: 4" in out["rootHtml"]
    assert "Manage service widgets" in out["rootHtml"]
    assert "Widgets for demo-local-agent-control-dashboard" in out["rootHtml"]
    assert "service-api-chat" in out["rootHtml"]
    assert "service-browser-panel" in out["rootHtml"]
    assert "service-health" in out["rootHtml"]
    assert "service-settings-review" in out["rootHtml"]
    assert "Queued widget events" in out["rootHtml"]
    assert "service.status.check" in out["rootHtml"]
    assert "action: check-local-service" in out["rootHtml"]
    assert "authorization" not in out["rootHtml"].lower()
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_music_walkthrough_is_visible_and_opens_sequencer_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runMusicWalkthrough")

    assert "Run music walkthrough" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_step_sequencer_piano_roll"}
    assert {"path": "api/spaces/widget/events?space_id=demo-step-sequencer-piano-roll", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-step-sequencer-piano-roll", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "Music Sequencer Smoke" in out["rootHtml"]
    assert "Action: music-pattern-seeded" in out["rootHtml"]
    assert "Music sequencer preview" in out["rootHtml"]
    assert "Pattern: 16 steps saved" in out["rootHtml"]
    assert "WebAudio: disabled until approved" in out["rootHtml"]
    assert "Piano roll: metadata-only" in out["rootHtml"]
    assert "Cleanup: planned-on-rerender" in out["rootHtml"]
    assert "Manage music widgets" in out["rootHtml"]
    assert "Widgets for demo-step-sequencer-piano-roll" in out["rootHtml"]
    assert "music-sequencer-grid" in out["rootHtml"]
    assert "music-synth-controls" in out["rootHtml"]
    assert "music-piano-roll" in out["rootHtml"]
    assert "music-notes" in out["rootHtml"]
    assert "Queued widget events" in out["rootHtml"]
    assert "audio.pattern.save" in out["rootHtml"]
    assert "authorization" not in out["rootHtml"].lower()
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_provider_setup_walkthrough_is_visible_and_opens_model_setup_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runProviderSetupWalkthrough")

    assert "Run provider setup walkthrough" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_provider_setup"}
    assert {"path": "api/spaces/widget/events?space_id=demo-provider-setup", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-provider-setup", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "Provider Setup Smoke" in out["rootHtml"]
    assert "Action: provider-setup-seeded" in out["rootHtml"]
    assert "Manage provider widgets" in out["rootHtml"]
    assert "Widgets for demo-provider-setup" in out["rootHtml"]
    assert "model-provider-status" in out["rootHtml"]
    assert "model-local-runtime" in out["rootHtml"]
    assert "model-settings-review" in out["rootHtml"]
    assert "model-next-steps" in out["rootHtml"]
    assert "Queued widget events" in out["rootHtml"]
    assert "provider.setup.review" in out["rootHtml"]
    assert "action: review-provider-setup" in out["rootHtml"]
    assert "authorization" not in out["rootHtml"].lower()
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_big_bang_walkthrough_is_visible_and_opens_widget_manager_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runBigBangWalkthrough")

    assert "Run Big Bang onboarding" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_big_bang_onboarding"}
    assert {"path": "api/spaces/widget/events?space_id=demo-big-bang-onboarding", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-big-bang-onboarding", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "Big Bang Onboarding Smoke" in out["rootHtml"]
    assert "Action: big-bang-onboarding-seeded" in out["rootHtml"]
    assert "Widgets for demo-big-bang-onboarding" in out["rootHtml"]
    assert "bigbang-welcome" in out["rootHtml"]
    assert "bigbang-demo-launcher" in out["rootHtml"]
    assert "bigbang-safety" in out["rootHtml"]
    assert "bigbang-next-steps" in out["rootHtml"]
    assert "authorization" not in out["rootHtml"].lower()
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_time_travel_walkthrough_is_visible_and_opens_widget_manager_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runTimeTravelWalkthrough")

    assert "Run time travel walkthrough" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_time_travel_restore"}
    assert {"path": "api/spaces/widget/events?space_id=demo-time-travel-restore", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-time-travel-restore", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "demo_time_travel_restore" in out["rootHtml"]
    assert "Time Travel Restore Smoke" in out["rootHtml"]
    assert "Action: restored" in out["rootHtml"]
    assert "Rollback point: yes" in out["rootHtml"]
    assert "Widgets for demo-time-travel-restore" in out["rootHtml"]
    assert "weather-current" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_admin_recovery_walkthrough_is_visible_and_opens_recovery_widget_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runAdminRecoveryWalkthrough")

    assert "Run admin recovery walkthrough" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_safe_admin_recovery"}
    assert {"path": "api/spaces/widget/events?space_id=demo-safe-admin-recovery", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-safe-admin-recovery", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "demo_safe_admin_recovery" in out["rootHtml"]
    assert "Admin Recovery Smoke" in out["rootHtml"]
    assert "Action: recovery-disabled" in out["rootHtml"]
    assert "Widgets for demo-safe-admin-recovery" in out["rootHtml"]
    assert "weather-current" in out["rootHtml"]
    assert "Recovery: disabled" in out["rootHtml"]
    assert "demo smoke recovery" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_browser_walkthrough_is_visible_and_opens_widget_manager_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runBrowserWalkthrough")

    assert "Run browser walkthrough" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_browser_cocontrol_google_or_test_site"}
    assert {"path": "api/spaces/widget/events?space_id=demo-browser-cocontrol-google-or-test-site", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-browser-cocontrol-google-or-test-site", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "Browser Co-control Smoke" in out["rootHtml"]
    assert "Action: browser-surface-seeded" in out["rootHtml"]
    assert "Widgets for demo-browser-cocontrol-google-or-test-site" in out["rootHtml"]
    assert "browser-panel" in out["rootHtml"]
    assert "browser-controls" in out["rootHtml"]
    assert "browser-notes" in out["rootHtml"]
    assert "Queued widget events" in out["rootHtml"]
    assert "browser.open_url" in out["rootHtml"]
    assert "action: open-test-site" in out["rootHtml"]
    assert "authorization" not in out["rootHtml"].lower()
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_research_walkthrough_is_visible_and_opens_widget_manager_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runResearchWalkthrough")

    assert "Run research walkthrough" in out["beforeHtml"]
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")
    assert run_post["method"] == "POST"
    assert json.loads(run_post["body"]) == {"demo": "demo_research_harness_pdf_export"}
    assert {"path": "api/spaces/widget/events?space_id=demo-research-harness-pdf-export", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/widgets?space_id=demo-research-harness-pdf-export", "method": "GET", "body": ""} in out["calls"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "Research Harness" in out["rootHtml"]
    assert "Action: pdf-export-requested" in out["rootHtml"]
    assert "Rollback verified: yes" in out["rootHtml"]
    assert "Widgets for demo-research-harness-pdf-export" in out["rootHtml"]
    assert "research-query" in out["rootHtml"]
    assert "research-summary" in out["rootHtml"]
    assert "Queued widget events" in out["rootHtml"]
    assert "widget.export.pdf" in out["rootHtml"]
    assert "note: [REDACTED]" in out["rootHtml"]
    assert "prompt: [REDACTED]" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_research_demo_smoke_shows_pdf_export_progress_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runResearchDemoParitySmoke")
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")

    assert json.loads(run_post["body"]) == {"demo": "demo_research_harness_pdf_export"}
    assert "Research harness PDF export" in out["beforeHtml"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "demo_research_harness_pdf_export" in out["rootHtml"]
    assert "Research Harness" in out["rootHtml"]
    assert "Action: pdf-export-requested" in out["rootHtml"]
    assert "Manage demo widgets" in out["rootHtml"]
    assert "Manage weather widget" not in out["rootHtml"]
    assert "Queued events: 1" in out["rootHtml"]
    assert "Rollback verified: yes" in out["rootHtml"]
    assert "Widgets: 5" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_notes_demo_smoke_shows_saved_note_preview_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runNotesDemoParitySmoke")
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")

    assert json.loads(run_post["body"]) == {"demo": "demo_notes_app"}
    assert "Notes app" in out["beforeHtml"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "demo_notes_app" in out["rootHtml"]
    assert "Notes App Smoke" in out["rootHtml"]
    assert "Action: notes-draft-saved" in out["rootHtml"]
    assert "Widgets: 4" in out["rootHtml"]
    assert "Saved notes preview" in out["rootHtml"]
    assert "Notes app checklist" in out["rootHtml"]
    assert "Folder list preview" in out["rootHtml"]
    assert "Attachment preview" in out["rootHtml"]
    assert "Attachments: 2" in out["rootHtml"]
    assert "Storage: agent-mediated" in out["rootHtml"]
    assert "demo-note.md · markdown · ready" in out["rootHtml"]
    assert "whiteboard.png · image · planned" in out["rootHtml"]
    assert "Folders: 2" in out["rootHtml"]
    assert "Active folder: Demo Project" in out["rootHtml"]
    assert "Inbox" in out["rootHtml"]
    assert "Demo Project" in out["rootHtml"]
    assert "Rename: metadata-only" in out["rootHtml"]
    assert "Create folder: metadata-only" in out["rootHtml"]
    assert "<li>Folder list ready</li>" in out["rootHtml"]
    assert "<li>Editor draft saved</li>" in out["rootHtml"]
    assert "<li>Markdown preview saved</li>" in out["rootHtml"]
    assert "<li>Attachments remain agent-mediated</li>" in out["rootHtml"]
    assert "<li>1. Folder list ready</li>" not in out["rootHtml"]
    assert "<li>2. Editor draft saved</li>" not in out["rootHtml"]
    assert "<li>3. Markdown preview saved</li>" not in out["rootHtml"]
    assert "<li>4. Attachments remain agent-mediated</li>" not in out["rootHtml"]
    assert "Demo note draft saved through typed Capy Spaces metadata." in out["rootHtml"]
    assert "This markdown preview was saved as metadata-only state." in out["rootHtml"]
    assert "Manage notes widgets" in out["rootHtml"]
    assert "Manage demo widgets" not in out["rootHtml"]
    assert "Manage weather widget" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_kanban_demo_smoke_shows_board_preview_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runKanbanDemoParitySmoke")
    run_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run")

    assert json.loads(run_post["body"]) == {"demo": "demo_kanban_board"}
    assert "Kanban board" in out["beforeHtml"]
    assert "Demo parity smoke passed" in out["rootHtml"]
    assert "demo_kanban_board" in out["rootHtml"]
    assert "Kanban Board Smoke" in out["rootHtml"]
    assert "Action: kanban-board-seeded" in out["rootHtml"]
    assert "Widgets: 4" in out["rootHtml"]
    assert "Kanban board preview" in out["rootHtml"]
    assert "Backlog" in out["rootHtml"]
    assert "Plan the first task" in out["rootHtml"]
    assert "Doing" in out["rootHtml"]
    assert "Build metadata-only board preview" in out["rootHtml"]
    assert "Done" in out["rootHtml"]
    assert "Install board template" in out["rootHtml"]
    assert "Manage kanban widgets" in out["rootHtml"]
    assert "Manage demo widgets" not in out["rootHtml"]
    assert "Manage weather widget" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_runs_all_demo_parity_smokes_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "runDemoParityAllSmokes")
    run_all_post = next(call for call in out["calls"] if call["path"] == "api/spaces/demo/run-all")

    assert "Run all smokes" in out["beforeHtml"]
    assert run_all_post["method"] == "POST"
    assert json.loads(run_all_post["body"]) == {}
    assert not any(
        call["path"] == "api/spaces/tool" and json.loads(call["body"] or "{}").get("action", "").startswith("space.demo")
        for call in out["calls"]
    )
    assert "Demo parity smoke suite passed" in out["rootHtml"]
    assert "5 / 5 metadata-only smokes passed" in out["rootHtml"]
    assert "demo_weather_widget" in out["rootHtml"]
    assert "demo_notes_app" in out["rootHtml"]
    assert "demo_kanban_board" in out["rootHtml"]
    assert "demo_research_harness_pdf_export" in out["rootHtml"]
    assert "demo_time_travel_restore" in out["rootHtml"]
    assert "persistence: checked" in out["rootHtml"]
    assert "Weather demo checklist" in out["rootHtml"]
    assert "Weather flow: chat answer recorded · widget created · reload verified" in out["rootHtml"]
    assert "Weather observation: Prague, CZ · 18 °C · partly cloudy · Agent bridge: 1 queued" in out["rootHtml"]
    assert "Notes app checklist" in out["rootHtml"]
    assert "Notes flow: folders 2 · active Demo Project · editor saved · markdown saved · attachments agent-mediated" in out["rootHtml"]
    assert "Kanban board checklist" in out["rootHtml"]
    assert "Kanban flow: columns 3 · cards 3 · drag/drop planned · card edits metadata-only" in out["rootHtml"]
    assert "Research harness checklist" in out["rootHtml"]
    assert "Research flow: PDF export queued · rollback verified · replayed after restore · restored widgets 5" in out["rootHtml"]
    assert "Observation summary" not in out["rootHtml"]
    assert "What is the weather in Prague?" not in out["rootHtml"]
    assert "Prague is partly cloudy at 18 °C" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET" not in out["rootHtml"]
    assert "UNTRUSTED_VALUE" not in out["rootHtml"]
    assert "UNTRUSTED_SOURCE" not in out["rootHtml"]


def test_spaces_ui_edit_space_posts_to_update_without_changing_space_id(driver_path):
    out = _run_spaces_scenario(driver_path, "editSpace")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/update")

    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_id": "lab",
        "updates": {"name": "Lab Edited", "description": "Updated"},
    }
    assert out["values"]["#capySpaceId"] == "lab"
    assert out["calls"][-1]["path"] == "api/spaces"


def test_spaces_ui_delete_space_posts_to_delete_and_refreshes_spaces(driver_path):
    out = _run_spaces_scenario(driver_path, "deleteSpaceConfirmed")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/delete")

    assert out["dialogs"]
    assert out["dialogs"][0]["danger"] is True
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "lab"}
    assert out["calls"][-1]["path"] == "api/spaces"


def test_spaces_ui_activate_space_posts_current_session_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "activateSpace")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/activate")

    assert "Clear from chat" in out["rootHtml"]
    assert "Active in chat" in out["rootHtml"]
    assert out["capySpaceSyncs"] == ["lab"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "lab", "session_id": "session-123"}
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]


def test_spaces_ui_clear_active_space_posts_current_session_and_refreshes_shell(driver_path):
    out = _run_spaces_scenario(driver_path, "clearActiveSpace")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/deactivate")

    assert "Active in chat" in out["beforeHtml"]
    assert "Clear from chat" in out["beforeHtml"]
    assert "Active in chat" not in out["rootHtml"]
    assert out["capySpaceSyncs"] == [None]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"session_id": "session-123"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]


def test_spaces_ui_delete_space_fails_closed_without_shared_dialog(driver_path):
    out = _run_spaces_scenario(driver_path, "deleteSpace")

    assert not any(call["path"] == "api/spaces/delete" for call in out["calls"])


def test_spaces_ui_cancelled_delete_space_does_not_post(driver_path):
    out = _run_spaces_scenario(driver_path, "deleteSpaceCancelled")

    assert out["dialogs"]
    assert not any(call["path"] == "api/spaces/delete" for call in out["calls"])


def test_spaces_ui_recovery_panel_lists_safe_space_metadata_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "recovery")

    assert {"path": "api/spaces/recovery", "method": "GET", "body": ""} in out["calls"]
    assert "Safe recovery" in out["recoveryHtml"]
    assert "Broken &lt;Space&gt;" in out["recoveryHtml"]
    assert "Widgets: 2" in out["recoveryHtml"]
    assert "Bad &lt;Widget&gt;" in out["recoveryHtml"]
    assert "Disabled Widget" in out["recoveryHtml"]
    assert "Disabled &lt;Space&gt;" in out["recoveryHtml"]
    assert "Space disabled: shell crash &lt;script&gt;ignored&lt;/script&gt;" in out["recoveryHtml"]
    assert "Disable space" in out["recoveryHtml"]
    assert "Enable space" in out["recoveryHtml"]
    assert "Ask Capy to repair Space" in out["recoveryHtml"]
    assert "Space repair queued: agent.repair · queued" in out["recoveryHtml"]
    assert "evt-space-re" in out["recoveryHtml"]
    assert "Disable widget" in out["recoveryHtml"]
    assert "Enable widget" in out["recoveryHtml"]
    assert "Ask Capy to repair" in out["recoveryHtml"]
    assert "Queued events: 1" in out["recoveryHtml"]
    assert "agent.repair · queued" in out["recoveryHtml"]
    assert "Event: evt-repair" in out["recoveryHtml"]
    assert "Restore revision" in out["recoveryHtml"]
    assert "Restore widget" in out["recoveryHtml"]
    assert 'data-capy-action="restoreRecoveryWidgetRevision"' in out["recoveryHtml"]
    assert 'data-widget-id="safe-widget"' in out["recoveryHtml"]
    assert 'data-widget-id="added-widget"' in out["recoveryHtml"]
    assert "raw-html-widget" not in out["recoveryHtml"]
    assert "script-widget" not in out["recoveryHtml"]
    assert "api_auth_widget" not in out["recoveryHtml"]
    assert "source-widget" not in out["recoveryHtml"]
    assert "secret-widget" not in out["recoveryHtml"]
    assert 'data-widget-id="removed-widget"' not in out["recoveryHtml"]
    assert "widget.recovery_disabled" in out["recoveryHtml"]
    assert "space.updated" in out["recoveryHtml"]
    assert "rev-before-break" in out["recoveryHtml"]
    assert "Preview: Broken safe checkpoint · 1 widget · Widgets: safe-widget / Safe Widget / markdown" in out["recoveryHtml"]
    assert "Preview: Broken current · 2 widgets · Widgets: bad-widget / Bad &lt;Widget&gt; / html, disabled-widget / Disabled Widget / markdown" in out["recoveryHtml"]
    assert "reason: [REDACTED]" in out["recoveryHtml"]
    assert "Disabled: render failed" in out["recoveryHtml"]
    assert "Generated widgets rendered: false" in out["recoveryHtml"]
    assert "Recovery hard gate" in out["recoveryHtml"]
    assert "metadata-only recovery · generated widgets not rendered · rollback controls available · disable and repair controls available" in out["recoveryHtml"]
    assert "Recovery summary: 2 spaces · 3 widgets · 1 disabled space · 1 disabled widget · 2 rollback points · 1 queued event · 3 modules · 1 disabled module" in out["recoveryHtml"]
    assert "Quarantined modules" in out["recoveryHtml"]
    assert "Safe Module" in out["recoveryHtml"]
    assert "Metadata-only module descriptor" in out["recoveryHtml"]
    assert "safe-module" in out["recoveryHtml"]
    assert "unsafe-module" in out["recoveryHtml"]
    assert "Safe blocked module" in out["recoveryHtml"]
    assert "Unsafe id should not become an action target" in out["recoveryHtml"]
    assert 'data-module-id="api_key"' not in out["recoveryHtml"]
    assert "Disabled: [REDACTED]" in out["recoveryHtml"]
    assert "/api/spaces/recovery" in out["recoveryHtml"]
    assert "/api/spaces/revision/restore-widget" in out["recoveryHtml"]
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]
    assert "SECRET" not in out["recoveryHtml"]


def test_spaces_ui_recovery_module_controls_use_shared_confirm_and_refresh(driver_path):
    out = _run_spaces_scenario(driver_path, "disableRecoveryModule")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/recovery/disable-module")

    assert "Disable module" in out["beforeHtml"]
    assert "Enable module" in out["beforeHtml"]
    assert out["dialogs"]
    assert out["dialogs"][0]["danger"] is True
    assert out["dialogs"][0]["confirmLabel"] == "Disable module"
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"module_id": "safe-module", "reason": "disabled from recovery panel"}
    assert out["calls"][-1]["path"] == "api/spaces/recovery"
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]
    assert "api_key" not in out["recoveryHtml"]
    assert "SECRET" not in out["recoveryHtml"]


def test_spaces_ui_recovery_disable_module_fails_closed_without_shared_dialog(driver_path):
    out = _run_spaces_scenario(driver_path, "disableRecoveryModuleNoDialog")

    assert not any(call["path"] == "api/spaces/recovery/disable-module" for call in out["calls"])


def test_spaces_ui_recovery_enable_module_uses_shared_confirm_and_refresh(driver_path):
    out = _run_spaces_scenario(driver_path, "enableRecoveryModule")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/recovery/enable-module")

    assert out["dialogs"]
    assert out["dialogs"][0]["danger"] is True
    assert out["dialogs"][0]["confirmLabel"] == "Enable module"
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"module_id": "unsafe-module", "reason": "enabled from recovery panel"}
    assert out["calls"][-1]["path"] == "api/spaces/recovery"
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]
    assert "api_key" not in out["recoveryHtml"]
    assert "SECRET" not in out["recoveryHtml"]


def test_spaces_ui_recovery_enable_module_cancel_does_not_post(driver_path):
    out = _run_spaces_scenario(driver_path, "enableRecoveryModuleCancelled")

    assert out["dialogs"]
    assert not any(call["path"] == "api/spaces/recovery/enable-module" for call in out["calls"])


def test_spaces_ui_recovery_disable_widget_uses_shared_confirm_and_refreshes(driver_path):
    out = _run_spaces_scenario(driver_path, "disableRecoveryWidget")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/recovery/disable-widget")

    assert out["dialogs"]
    assert out["dialogs"][0]["danger"] is True
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "broken", "widget_id": "bad-widget", "reason": "disabled from recovery panel"}
    assert out["calls"][-1]["path"] == "api/spaces/recovery"
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]


def test_spaces_ui_recovery_disable_widget_fails_closed_without_shared_dialog(driver_path):
    out = _run_spaces_scenario(driver_path, "disableRecoveryWidgetNoDialog")

    assert not any(call["path"] == "api/spaces/recovery/disable-widget" for call in out["calls"])


def test_spaces_ui_recovery_repair_widget_queues_agent_event_from_safe_panel(driver_path):
    out = _run_spaces_scenario(driver_path, "repairRecoveryWidget")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/event")
    body = json.loads(post["body"])

    assert "Ask Capy to repair" in out["beforeHtml"]
    assert out["dialogs"]
    assert out["dialogs"][0]["title"] == "Ask Capy to repair widget"
    assert post["method"] == "POST"
    assert body == {
        "space_id": "broken",
        "widget_id": "bad-widget",
        "event_name": "agent.repair",
        "prompt": "Patch the broken renderer without exposing secrets",
        "payload": {"source": "recovery-panel", "action": "repair", "widget_title": "Bad <Widget>"},
    }
    assert out["calls"][-1]["path"] == "api/spaces/recovery"
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]
    assert "SECRET" not in out["recoveryHtml"]


def test_spaces_ui_recovery_repair_widget_fails_closed_without_shared_prompt(driver_path):
    out = _run_spaces_scenario(driver_path, "repairRecoveryWidgetNoPrompt")

    assert not any(call["path"] == "api/spaces/widget/event" for call in out["calls"])


def test_spaces_ui_recovery_repair_space_queues_metadata_only_event_from_safe_panel(driver_path):
    out = _run_spaces_scenario(driver_path, "repairRecoverySpace")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/recovery/repair-space")
    body = json.loads(post["body"])

    assert "Ask Capy to repair Space" in out["beforeHtml"]
    assert out["dialogs"]
    assert out["dialogs"][0]["title"] == "Ask Capy to repair Space"
    assert out["dialogs"][0]["confirmLabel"] == "Queue repair"
    assert post["method"] == "POST"
    assert body == {
        "space_id": "broken",
        "prompt": "Repair the Space shell without exposing renderer secrets",
        "payload": {"source": "recovery-panel", "action": "repair-space"},
    }
    assert out["calls"][-1]["path"] == "api/spaces/recovery"
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]
    assert "api_key" not in out["recoveryHtml"]
    assert "SECRET" not in out["recoveryHtml"]


def test_spaces_ui_recovery_repair_space_fails_closed_without_shared_prompt(driver_path):
    out = _run_spaces_scenario(driver_path, "repairRecoverySpaceNoPrompt")

    assert not any(call["path"] == "api/spaces/recovery/repair-space" for call in out["calls"])


def test_spaces_ui_recovery_restore_revision_uses_shared_confirm_and_refreshes(driver_path):
    out = _run_spaces_scenario(driver_path, "restoreRecoveryRevision")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/revision/restore")

    assert out["dialogs"]
    assert out["dialogs"][0]["danger"] is True
    assert out["dialogs"][0]["confirmLabel"] == "Restore revision"
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "broken", "event_id": "rev-before-break"}
    assert out["calls"][-1]["path"] == "api/spaces/recovery"
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]
    assert "api_key" not in out["recoveryHtml"]
    assert "SECRET" not in out["recoveryHtml"]


def test_spaces_ui_recovery_restore_revision_fails_closed_without_shared_dialog(driver_path):
    out = _run_spaces_scenario(driver_path, "restoreRecoveryRevisionNoDialog")

    assert not any(call["path"] == "api/spaces/revision/restore" for call in out["calls"])


def test_spaces_ui_recovery_restore_widget_revision_uses_shared_confirm_and_refreshes(driver_path):
    out = _run_spaces_scenario(driver_path, "restoreRecoveryWidgetRevision")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/revision/restore-widget")

    assert out["dialogs"]
    assert out["dialogs"][0]["danger"] is True
    assert out["dialogs"][0]["confirmLabel"] == "Restore widget"
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "broken", "event_id": "rev-before-break", "widget_id": "safe-widget"}
    assert out["calls"][-1]["path"] == "api/spaces/recovery"
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]
    assert "api_key" not in out["recoveryHtml"]
    assert "api_auth" not in out["recoveryHtml"]
    assert "SECRET" not in out["recoveryHtml"]


def test_spaces_ui_recovery_restore_widget_revision_fails_closed_without_shared_dialog(driver_path):
    out = _run_spaces_scenario(driver_path, "restoreRecoveryWidgetRevisionNoDialog")

    assert not any(call["path"] == "api/spaces/revision/restore-widget" for call in out["calls"])


def test_spaces_ui_recovery_restore_widget_revision_cancel_does_not_post(driver_path):
    out = _run_spaces_scenario(driver_path, "restoreRecoveryWidgetRevisionCancelled")

    assert out["dialogs"]
    assert not any(call["path"] == "api/spaces/revision/restore-widget" for call in out["calls"])


def test_spaces_ui_recovery_disable_space_uses_shared_confirm_and_refreshes(driver_path):
    out = _run_spaces_scenario(driver_path, "disableRecoverySpace")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/recovery/disable-space")

    assert out["dialogs"]
    assert out["dialogs"][0]["danger"] is True
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "broken", "reason": "disabled from recovery panel"}
    assert out["calls"][-1]["path"] == "api/spaces/recovery"
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]


def test_spaces_ui_recovery_disable_space_fails_closed_without_shared_dialog(driver_path):
    out = _run_spaces_scenario(driver_path, "disableRecoverySpaceNoDialog")

    assert not any(call["path"] == "api/spaces/recovery/disable-space" for call in out["calls"])


def test_spaces_ui_recovery_export_yaml_posts_space_id_and_renders_safe_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "recoveryExportSpaceYaml")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/export")

    assert "Export YAML" in out["beforeHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "broken", "format": "yaml"}
    assert "Space Agent export ready" in out["recoveryHtml"]
    assert "Format: yaml" in out["recoveryHtml"]
    assert "broken-space-agent.yaml" in out["recoveryHtml"]
    assert "space_yaml" not in out["recoveryHtml"]
    assert "widgets/weather.yaml" not in out["recoveryHtml"]
    assert "zip_b64" not in out["recoveryHtml"]
    assert "archive_b64" not in out["recoveryHtml"]
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]
    assert "api_key" not in out["recoveryHtml"]
    assert "SECRET" not in out["recoveryHtml"]


def test_spaces_ui_recovery_export_zip_posts_space_id_and_renders_safe_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "recoveryExportSpaceZip")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/export")

    assert "Export ZIP" in out["beforeHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "broken", "format": "zip"}
    assert "Space Agent export ready" in out["recoveryHtml"]
    assert "Format: zip" in out["recoveryHtml"]
    assert "broken-space-agent.zip" in out["recoveryHtml"]
    assert "space_yaml" not in out["recoveryHtml"]
    assert "widgets/weather.yaml" not in out["recoveryHtml"]
    assert "zip_b64" not in out["recoveryHtml"]
    assert "archive_b64" not in out["recoveryHtml"]
    assert "U0VDUkVUX1pJUF9JTUFHSU5BUlk" not in out["recoveryHtml"]
    assert "U0VDUkVUX0FSQ0hJVkVfSU1BR0lOQVJZ" not in out["recoveryHtml"]
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]
    assert "api_key" not in out["recoveryHtml"]
    assert "SECRET" not in out["recoveryHtml"]


def test_spaces_ui_recovery_enable_widget_uses_shared_confirm_and_refreshes(driver_path):
    out = _run_spaces_scenario(driver_path, "enableRecoveryWidget")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/recovery/enable-widget")

    assert out["dialogs"]
    assert out["dialogs"][0]["danger"] is True
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "broken", "widget_id": "disabled-widget", "reason": "enabled from recovery panel"}
    assert out["calls"][-1]["path"] == "api/spaces/recovery"
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]
    assert "api_key" not in out["recoveryHtml"]
    assert "SECRET" not in out["recoveryHtml"]


def test_spaces_ui_recovery_enable_widget_cancel_does_not_post(driver_path):
    out = _run_spaces_scenario(driver_path, "enableRecoveryWidgetCancelled")

    assert out["dialogs"]
    assert not any(call["path"] == "api/spaces/recovery/enable-widget" for call in out["calls"])


def test_spaces_ui_recovery_enable_space_uses_shared_confirm_and_refreshes(driver_path):
    out = _run_spaces_scenario(driver_path, "enableRecoverySpace")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/recovery/enable-space")

    assert out["dialogs"]
    assert out["dialogs"][0]["danger"] is True
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "disabled-space", "reason": "enabled from recovery panel"}
    assert out["calls"][-1]["path"] == "api/spaces/recovery"
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]
    assert "api_key" not in out["recoveryHtml"]
    assert "SECRET" not in out["recoveryHtml"]


def test_spaces_ui_recovery_enable_space_cancel_does_not_post(driver_path):
    out = _run_spaces_scenario(driver_path, "enableRecoverySpaceCancelled")

    assert out["dialogs"]
    assert not any(call["path"] == "api/spaces/recovery/enable-space" for call in out["calls"])


def test_spaces_ui_opens_space_detail_without_rendering_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "openSpaceDetail")

    assert {"path": "api/spaces/get?space_id=lab", "method": "GET", "body": ""} in out["calls"]
    assert {"path": "api/spaces/revisions?space_id=lab", "method": "GET", "body": ""} in out["calls"]
    assert "Lab &lt;Detail&gt;" in out["rootHtml"]
    assert "Unsafe &lt;detail&gt;" in out["rootHtml"]
    assert "&lt;Weather&gt;" in out["rootHtml"]
    assert "x12 y3 · 5×4" in out["rootHtml"]
    assert "Export YAML" in out["rootHtml"]
    assert "Export ZIP" in out["rootHtml"]
    assert "Shared data" in out["rootHtml"]
    assert "research-summary" in out["rootHtml"]
    assert "Delete data slot" in out["rootHtml"]
    assert "title: Safe research findings" in out["rootHtml"]
    assert "notes: ready for widget cooperation" in out["rootHtml"]
    assert "source_widget: weather" in out["rootHtml"]
    assert "Revision history" in out["rootHtml"]
    assert "widget.updated" in out["rootHtml"]
    assert "space.created" in out["rootHtml"]
    assert "rev2" in out["rootHtml"]
    assert "widget_id: weather" in out["rootHtml"]
    assert "fields: title, layout" in out["rootHtml"]
    assert "note: [REDACTED]" in out["rootHtml"]
    assert "Preview: Lab patched · 1 widget" in out["rootHtml"]
    assert "Widgets: weather / Weather patched / markdown" in out["rootHtml"]
    assert "Preview: Lab &lt;Detail&gt; · 1 widget" in out["rootHtml"]
    assert "Widgets: weather / &lt;Weather&gt; / markdown" in out["rootHtml"]
    assert "Diff: restore changes 1 field, removes 1 widget, updates 1 widget" in out["rootHtml"]
    assert "Fields: description" in out["rootHtml"]
    assert "Remove widgets: notes" in out["rootHtml"]
    assert "Update widgets: weather" in out["rootHtml"]
    assert "name: Lab &lt;Detail&gt;" in out["rootHtml"]
    assert 'data-capy-action="restoreRevision"' in out["rootHtml"]
    assert 'data-capy-action="restoreWidgetRevision"' in out["rootHtml"]
    assert 'data-widget-id="weather"' in out["rootHtml"]
    assert "Restore widget" in out["rootHtml"]
    assert "Restore" in out["rootHtml"]
    assert 'data-capy-action="rollbackRevision"' not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_delete_shared_data_fails_closed_without_shared_confirm(driver_path):
    out = _run_spaces_scenario(driver_path, "deleteSharedDataNoDialog")

    assert "Delete data slot" in out["beforeHtml"]
    assert not any(call["path"] == "api/spaces/data/delete" for call in out["calls"])
    assert out["dialogs"] == []


def test_spaces_ui_delete_shared_data_cancel_does_not_send_delete(driver_path):
    out = _run_spaces_scenario(driver_path, "deleteSharedDataCancelled")

    assert out["dialogs"]
    assert out["dialogs"][0]["title"] == "Delete shared data slot?"
    assert not any(call["path"] == "api/spaces/data/delete" for call in out["calls"])


def test_spaces_ui_delete_shared_data_confirm_posts_key_only_and_refreshes_detail(driver_path):
    out = _run_spaces_scenario(driver_path, "deleteSharedDataConfirmed")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/data/delete")

    assert out["dialogs"]
    assert out["dialogs"][0]["title"] == "Delete shared data slot?"
    assert out["dialogs"][0]["confirmLabel"] == "Delete data slot"
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "lab", "key": "research-summary"}
    assert out["calls"][-2]["path"] == "api/spaces/get?space_id=lab"
    assert out["calls"][-1]["path"] == "api/spaces/revisions?space_id=lab"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_restore_revision_uses_shared_confirm_and_reload_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "restoreRevisionConfirmed")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/revision/restore")

    assert out["dialogs"]
    assert out["dialogs"][0]["danger"] is True
    assert "Restore" in out["dialogs"][0]["confirmLabel"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "lab", "event_id": "rev1"}
    assert out["calls"][-2]["path"] == "api/spaces/get?space_id=lab"
    assert out["calls"][-1]["path"] == "api/spaces/revisions?space_id=lab"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_restore_widget_revision_posts_widget_only_and_refreshes_detail(driver_path):
    out = _run_spaces_scenario(driver_path, "restoreWidgetRevisionConfirmed")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/revision/restore-widget")

    assert out["dialogs"]
    assert out["dialogs"][0]["title"] == "Restore widget revision?"
    assert out["dialogs"][0]["danger"] is True
    assert out["dialogs"][0]["confirmLabel"] == "Restore widget"
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "lab", "event_id": "rev1", "widget_id": "weather"}
    assert out["calls"][-2]["path"] == "api/spaces/get?space_id=lab"
    assert out["calls"][-1]["path"] == "api/spaces/revisions?space_id=lab"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_restore_widget_revision_fails_closed_without_shared_dialog(driver_path):
    out = _run_spaces_scenario(driver_path, "restoreWidgetRevisionNoDialog")

    assert not any(call["path"] == "api/spaces/revision/restore-widget" for call in out["calls"])



def test_spaces_ui_restore_revision_fails_closed_without_shared_dialog(driver_path):
    out = _run_spaces_scenario(driver_path, "restoreRevisionNoDialog")

    assert not any(call["path"] == "api/spaces/revision/restore" for call in out["calls"])


def test_spaces_ui_cancelled_restore_revision_does_not_post(driver_path):
    out = _run_spaces_scenario(driver_path, "restoreRevisionCancelled")

    assert out["dialogs"]
    assert not any(call["path"] == "api/spaces/revision/restore" for call in out["calls"])


def test_spaces_ui_export_yaml_posts_space_id_and_renders_safe_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "exportSpaceYaml")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/export")

    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "lab", "format": "yaml"}
    assert "Space Agent export ready" in out["rootHtml"]
    assert "Format: yaml" in out["rootHtml"]
    assert "lab-space-agent.yaml" in out["rootHtml"]
    assert "space_yaml" not in out["rootHtml"]
    assert "widgets/weather.yaml" not in out["rootHtml"]
    assert "zip_b64" not in out["rootHtml"]
    assert "archive_b64" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_export_zip_posts_space_id_and_renders_safe_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "exportSpaceZip")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/export")

    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "lab", "format": "zip"}
    assert "Space Agent export ready" in out["rootHtml"]
    assert "Format: zip" in out["rootHtml"]
    assert "lab-space-agent.zip" in out["rootHtml"]
    assert "space_yaml" not in out["rootHtml"]
    assert "widgets/weather.yaml" not in out["rootHtml"]
    assert "zip_b64" not in out["rootHtml"]
    assert "archive_b64" not in out["rootHtml"]
    assert "U0VDUkVUX1pJUF9JTUFHSU5BUlk" not in out["rootHtml"]
    assert "U0VDUkVUX0FSQ0hJVkVfSU1BR0lOQVJZ" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "token" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_import_yaml_posts_safe_payload_and_renders_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "importSpaceAgentYaml")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/import")

    assert "Import Space Agent YAML" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_yaml": "id: imported-lab\nname: Imported Lab\ndescription: Imported safely\n",
        "widgets": {"widgets/weather.yaml": "id: weather\ntitle: Weather\ntype: html\nrenderer: <script>bad()</script>\napi_key: SECRET"},
    }
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "Space Agent import ready" in out["rootHtml"]
    assert "Imported Lab" in out["rootHtml"]
    assert "imported-lab" in out["rootHtml"]
    assert "Weather" in out["rootHtml"]
    assert "1 widget" in out["rootHtml"]
    assert "Import warnings" in out["rootHtml"]
    assert "space.current.widget.patch" in out["rootHtml"]
    assert "space_yaml" not in out["rootHtml"]
    assert "widgets/weather.yaml" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_import_result_redacts_unsafe_display_metadata_from_backend_response(driver_path):
    out = _run_spaces_scenario(driver_path, "importSpaceAgentHostileYaml")

    assert "Space Agent import ready" in out["rootHtml"]
    assert "imported-lab" in out["rootHtml"]
    assert "Weather" in out["rootHtml"]
    assert "2 widgets" in out["rootHtml"]
    assert "Import warnings" in out["rootHtml"]
    assert "space.current.widget.patch" in out["rootHtml"]
    assert "Unsupported Space Agent API reference omitted during import." in out["rootHtml"]
    assert "Space Agent package warning omitted pending sandbox review." in out["rootHtml"]
    assert "renderer-panel" not in out["rootHtml"]
    assert "api_auth" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "SECRET_VALUE_DO_NOT_LEAK" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "&lt;script" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "generated_code" not in out["rootHtml"]
    assert "raw_prompt" not in out["rootHtml"]
    assert "SOURCE_LEAK_SENTINEL" not in out["rootHtml"]
    assert "DATA_LEAK_SENTINEL" not in out["rootHtml"]


def test_spaces_ui_import_zip_posts_archive_only_and_renders_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "importSpaceAgentZip")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/import")

    assert "Import Space Agent ZIP" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "archive_b64": "UEsDBBQAAAAIAAxTSFsAAAAAAAAAAAAAAAALAAAAc3BhY2UueWFtbA==",
    }
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "Space Agent import ready" in out["rootHtml"]
    assert "Imported ZIP Lab" in out["rootHtml"]
    assert "imported-zip-lab" in out["rootHtml"]
    assert "1 widget" in out["rootHtml"]
    assert "Import warnings" in out["rootHtml"]
    assert "space.current.widget.patch" in out["rootHtml"]
    assert "archive_b64" not in out["rootHtml"]
    assert "UEsDBBQ" not in out["rootHtml"]
    assert "space_yaml" not in out["rootHtml"]
    assert "widgets/weather.yaml" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_reset_big_bang_uses_shared_confirm_and_renders_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "resetBigBangOnboardingConfirmed")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/reset")

    assert "Reset Big Bang onboarding" in out["beforeHtml"]
    assert out["dialogs"]
    assert out["dialogs"][0]["danger"] is True
    assert "Reset" in out["dialogs"][0]["confirmLabel"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "big-bang", "space_id": "big-bang-onboarding"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "Big Bang Onboarding" in out["rootHtml"]
    assert "Welcome to Capy Spaces" in out["rootHtml"]
    assert "4 widgets" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_reset_big_bang_fails_closed_without_shared_dialog(driver_path):
    out = _run_spaces_scenario(driver_path, "resetBigBangOnboardingNoDialog")

    assert not any(call["path"] == "api/spaces/templates/reset" for call in out["calls"])


def test_spaces_ui_cancelled_reset_big_bang_does_not_post(driver_path):
    out = _run_spaces_scenario(driver_path, "resetBigBangOnboardingCancelled")

    assert out["dialogs"]
    assert not any(call["path"] == "api/spaces/templates/reset" for call in out["calls"])


def test_spaces_ui_renders_trusted_system_widgets_without_generated_content(driver_path):
    out = _run_spaces_scenario(driver_path, "systemWidgetShell")

    assert "Trusted WebUI system widgets" in out["rootHtml"]
    assert "system.chat" in out["rootHtml"]
    assert "system.workspaces" in out["rootHtml"]
    assert "system.tasks" in out["rootHtml"]
    assert "system.memory" in out["rootHtml"]
    assert "system.settings" in out["rootHtml"]
    assert "auth/settings/recovery shell remains outside" in out["rootHtml"]
    assert out["switchedPanels"] == ["chat", "settings"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_adds_trusted_system_widget_to_active_space_metadata_only(driver_path):
    out = _run_spaces_scenario(driver_path, "addSystemWidgetToActiveSpace")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/system-widget/upsert")

    assert "Add to active Space" in out["beforeHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {
        "space_id": "lab",
        "panel": "chat",
        "layout": {"x": 0, "y": 0, "w": 12, "h": 6},
    }
    assert out["calls"][-1]["path"] == "api/spaces/widgets?space_id=lab"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_creator_preview_gate_uses_tool_api_without_leaking_prompt_or_generated_fields(driver_path):
    out = _run_spaces_scenario(driver_path, "creatorPreviewGate")

    assert "Safe creator loop" in out["beforeHtml"]
    assert "Preview bounded spec" in out["beforeHtml"]
    preview_call = next(call for call in out["calls"] if call["path"] == "api/spaces/tool" and "space.creator.preview" in call["body"])
    preview_body = json.loads(preview_call["body"])
    assert preview_body == {
        "action": "space.creator.preview",
        "prompt": "Create an ops dashboard without leaking SECRET_VALUE_DO_NOT_LEAK or <script>bad()</script>",
    }
    assert "Creator preview ready" in out["rootHtml"]
    assert "stored: false" in out["rootHtml"]
    assert "executed: false" in out["rootHtml"]
    assert "sandbox preview required" in out["rootHtml"]
    assert "visual QA required" in out["rootHtml"]
    assert "Approve revisioned commit" in out["rootHtml"]
    assert "Creator Lab &lt;Safe&gt;" in out["rootHtml"]
    assert "Summary &lt;Widget&gt;" in out["rootHtml"]
    assert "SECRET_VALUE_DO_NOT_LEAK" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "raw_prompt" not in out["rootHtml"]
    assert "generated_code" not in out["rootHtml"]


def test_creator_preview_omits_unsafe_ids_and_commit_action(driver_path):
    out = _run_spaces_scenario(driver_path, "creatorPreviewUnsafeIds")

    assert "Creator preview ready" in out["rootHtml"]
    assert "Draft Space" in out["rootHtml"]
    assert "Unsafe Widget" in out["rootHtml"]
    assert "Preview: unnamed snapshot · 2 widgets · Widgets: Safe Widget / status, safe-widget / Safe Widget Two / status" in out["rootHtml"]
    assert "Add widgets: safe-widget" in out["rootHtml"]
    assert "Update widgets: safe-update" in out["rootHtml"]
    assert "preview/../escape" not in out["rootHtml"]
    assert "creator/../lab" not in out["rootHtml"]
    assert "../widget" not in out["rootHtml"]
    assert "creator/../old-widget" not in out["rootHtml"]
    assert "../update-widget" not in out["rootHtml"]
    assert "Remove widgets:" not in out["rootHtml"]
    assert "Approve revisioned commit" not in out["rootHtml"]
    assert "data-preview-id" not in out["rootHtml"]
    assert "SECRET_VALUE_DO_NOT_LEAK" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "raw_prompt" not in out["rootHtml"]
    assert "generated_code" not in out["rootHtml"]


def test_creator_preview_can_target_existing_space_and_render_revision_diff_safely(driver_path):
    out = _run_spaces_scenario(driver_path, "creatorPreviewExistingSpace")

    assert "Target existing Space ID" in out["beforeHtml"]
    preview_call = next(call for call in out["calls"] if call["path"] == "api/spaces/tool" and "space.creator.preview" in call["body"])
    preview_body = json.loads(preview_call["body"])
    assert preview_body == {
        "action": "space.creator.preview",
        "prompt": "Create an ops dashboard without leaking SECRET_VALUE_DO_NOT_LEAK or <script>bad()</script>",
        "space_id": "existing-creator-lab",
    }
    assert "Creator preview ready" in out["rootHtml"]
    assert "Revision preview" in out["rootHtml"]
    assert "Existing Creator Lab Revised" in out["rootHtml"]
    assert "Space ID: existing-creator-lab" in out["rootHtml"]
    assert "Preview: Existing Creator Lab Revised · 1 widget · Widgets: latest-panel / Latest Panel / status" in out["rootHtml"]
    assert "Diff: restore changes 3 fields, adds 1 widget, removes 1 widget" in out["rootHtml"]
    assert "Fields: description, agent_instructions, shared_data" in out["rootHtml"]
    assert "Add widgets: latest-panel" in out["rootHtml"]
    assert "Remove widgets: old-panel" in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "SECRET_VALUE_DO_NOT_LEAK" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "raw_prompt" not in out["rootHtml"]
    assert "generated_code" not in out["rootHtml"]


def test_creator_commit_requires_shared_confirm_and_revision_gates(driver_path):
    out = _run_spaces_scenario(driver_path, "creatorCommitConfirmed")

    commit_call = next(call for call in out["calls"] if call["path"] == "api/spaces/tool" and "space.creator.commit" in call["body"])
    assert json.loads(commit_call["body"]) == {
        "action": "space.creator.commit",
        "preview_id": "preview-safe-1",
        "sandbox_previewed": True,
        "visual_qa_passed": True,
        "approve_commit": True,
    }
    assert out["dialogs"]
    assert "Commit creator preview?" in json.dumps(out["dialogs"])
    assert "Creator commit saved" in out["rootHtml"]
    assert "stored: true" in out["rootHtml"]
    assert "executed: false" in out["rootHtml"]
    assert "revisioned-commit" in out["rootHtml"]
    assert "rev-creator-commit" in out["rootHtml"]
    assert "Open committed Space" in out["rootHtml"]
    assert "Manage committed widgets" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="creator-lab"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="creator-lab"' in out["rootHtml"]
    assert "SECRET_VALUE_DO_NOT_LEAK" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()


def test_creator_commit_existing_space_renders_revision_receipt_diff_safely(driver_path):
    out = _run_spaces_scenario(driver_path, "creatorCommitExistingSpaceReceipt")

    commit_call = next(call for call in out["calls"] if call["path"] == "api/spaces/tool" and "space.creator.commit" in call["body"])
    assert json.loads(commit_call["body"]) == {
        "action": "space.creator.commit",
        "preview_id": "preview-existing-safe-1",
        "sandbox_previewed": True,
        "visual_qa_passed": True,
        "approve_commit": True,
    }
    assert out["dialogs"]
    assert "Creator commit saved" in out["rootHtml"]
    assert "Revision preview" in out["rootHtml"]
    assert "Existing Creator Lab Revised" in out["rootHtml"]
    assert "Space ID: existing-creator-lab" in out["rootHtml"]
    assert "Preview: Existing Creator Lab Revised · 1 widget · Widgets: latest-panel / Latest Panel / status" in out["rootHtml"]
    assert "Diff: restore changes 3 fields, adds 1 widget, removes 1 widget" in out["rootHtml"]
    assert "Fields: description, agent_instructions, shared_data" in out["rootHtml"]
    assert "Add widgets: latest-panel" in out["rootHtml"]
    assert "Remove widgets: old-panel" in out["rootHtml"]
    assert "Open committed Space" in out["rootHtml"]
    assert "Manage committed widgets" in out["rootHtml"]
    assert 'data-capy-action="openSpace" data-space-id="existing-creator-lab"' in out["rootHtml"]
    assert 'data-capy-action="loadWidgets" data-space-id="existing-creator-lab"' in out["rootHtml"]
    assert "SECRET_VALUE_DO_NOT_LEAK" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()
    assert "raw_prompt" not in out["rootHtml"]
    assert "generated_code" not in out["rootHtml"]


def test_creator_commit_omits_followup_actions_for_unsafe_space_id(driver_path):
    out = _run_spaces_scenario(driver_path, "creatorCommitUnsafeSpaceId")

    assert "Creator commit saved" in out["rootHtml"]
    assert "Creator Lab &lt;Safe&gt;" in out["rootHtml"]
    assert "creator/../lab" not in out["rootHtml"]
    assert "Open committed Space" not in out["rootHtml"]
    assert "Manage committed widgets" not in out["rootHtml"]
    assert 'data-space-id="creator/../lab"' not in out["rootHtml"]
    assert "SECRET_VALUE_DO_NOT_LEAK" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"].lower()


def test_creator_commit_fails_closed_without_shared_confirm_dialog(driver_path):
    out = _run_spaces_scenario(driver_path, "creatorCommitNoDialog")

    assert not any(call["path"] == "api/spaces/tool" and "space.creator.commit" in call["body"] for call in out["calls"])
    assert "Creator preview ready" in out["rootHtml"]
