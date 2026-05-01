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
        { demo: 'demo_time_travel_restore', template: 'big-bang', title: 'Time travel rollback', mode: 'metadata-only-smoke', source: 'SECRET_SOURCE' },
      ],
    });
  }
  if (path === 'api/spaces/demo/run') {
    const body = opts.body ? JSON.parse(opts.body) : {};
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
  if (path === 'api/spaces/demo/run-all') {
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
  }
  if (path === 'api/spaces/recovery') {
    return response({
      enabled: true,
      generated_widgets_rendered: false,
      spaces: [
        {
          space_id: 'broken',
          name: 'Broken <Space>',
          description: 'Recover without <script>running</script>',
          widget_count: 2,
          revision_event_id: 'rev-broken',
          disabled: false,
          disabled_reason: '',
          renderer: '<script>bad()</script>',
          widgets: [
            { id: 'bad-widget', kind: 'html', title: 'Bad <Widget>', disabled: false, renderer: '<script>bad()</script>' },
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
  if (path === 'api/spaces/widgets?space_id=lab') {
    const minimized = scenario === 'restoreWidget';
    return response({ widgets: [{ id: 'weather', kind: 'markdown', title: '<Weather>', layout: { x: 12, y: 3, w: 5, h: 4, minimized: minimized }, renderer: '<script>bad()</script>' }] });
  }
  if (path === 'api/spaces/widget/events?space_id=lab') {
    return response({ events: [
      { event_id: 'evt-refresh', event_name: 'widget.refresh', widget_id: 'weather', status: 'queued', created_at: 1710000100, payload_summary: { action: 'refresh', note: 'Authorization: Bearer SECRET_VALUE_DO_NOT_LEAK' }, renderer: '<script>bad()</script>', api_key: 'SECRET' },
      { event_id: 'evt-agent', event_name: 'agent.prompt', widget_id: 'weather', status: 'queued', created_at: 1710000000, prompt_preview: 'Use token SECRET_VALUE_DO_NOT_LEAK', payload_summary: { query: 'forecast' } },
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
        interaction: { refresh: 'agent-mediated', dangerous_html: '<script>bad()</script>' },
        permissions: { network: 'agent-mediated', token: 'SECRET_VALUE_DO_NOT_LEAK', credential: 'SECRET_VALUE_DO_NOT_LEAK' },
      },
      renderer: '<script>bad()</script>',
      html: '<img src=x onerror=bad()>',
      data: { api_key: 'SECRET' },
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
      { event_id: 'rev2', event_type: 'widget.updated', space_id: 'lab', created_at: 1710000000, details: { widget_id: 'weather', fields: ['title', 'layout'], note: 'Authorization: Bearer SECRET_VALUE_DO_NOT_LEAK', renderer: '<script>bad()</script>', api_key: 'SECRET' } },
      { event_id: 'rev1', event_type: 'space.created', space_id: 'lab', created_at: 1709999900, details: { name: 'Lab <Detail>' } },
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
    return response({ queued: true, space_id: 'lab', widget_id: 'weather', event_name: 'agent.prompt', event_id: 'evt1' });
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
      filename: (body.format === 'zip') ? 'lab-space-agent.zip' : 'lab-space-agent.yaml',
      space_yaml: 'id: lab\nname: Lab\nrenderer: <script>bad()</script>\napi_key: SECRET',
      widgets: {'widgets/weather.yaml': 'id: weather\nscript: <script>bad()</script>\ntoken: SECRET'},
      zip_b64: body.format === 'zip' ? 'U0VDUkVUX1pJUF9JTUFHSU5BUlk=' : undefined,
    });
  }
  if (path === 'api/spaces/import') {
    const body = opts.body ? JSON.parse(opts.body) : {};
    const isZip = !!body.archive_b64;
    return response({
      ok: true,
      source: isZip ? 'space-agent-zip' : 'space-agent-yaml',
      space: { space_id: isZip ? 'imported-zip-lab' : 'imported-lab', name: isZip ? 'Imported ZIP Lab' : 'Imported Lab', description: 'Imported safely', widget_count: 1, revision_event_id: 'rev-import' },
      imported_widgets: [{ id: 'weather', kind: 'html', title: 'Weather', renderer: '<script>bad()</script>', api_key: 'SECRET' }],
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
  } else if (scenario === 'requestWidgetPdfExport') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('viewWidgetDetails', { spaceId: 'lab', widgetId: 'weather' });
    beforeHtml = root.innerHTML;
    await click('requestWidgetPdfExport', { spaceId: 'lab', widgetId: 'weather', widgetTitle: '<Weather>' });
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
    assert "x12 y3 · 5×4" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert {"path": "api/spaces/widgets?space_id=lab", "method": "GET", "body": ""} in out["calls"]


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
    assert "Widget details" in out["rootHtml"]
    assert "Back to widgets" in out["rootHtml"]
    assert "data-capy-action=\"loadWidgets\"" in out["rootHtml"]
    assert "&lt;Weather&gt;" in out["rootHtml"]
    assert "markdown" in out["rootHtml"]
    assert "x12 y3 · 5×4" in out["rootHtml"]
    assert "content_status: agent-managed-empty" in out["rootHtml"]
    assert "export: pdf" in out["rootHtml"]
    assert "interaction: refresh" in out["rootHtml"]
    assert "permissions: network" in out["rootHtml"]
    assert "credential" not in out["rootHtml"].lower()
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "onerror" not in out["rootHtml"]
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
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]


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


def test_spaces_ui_install_weather_demo_posts_template_and_refreshes_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "installWeatherDemo")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install weather demo" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "weather"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]


def test_spaces_ui_install_research_harness_posts_template_and_refreshes_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "installResearchHarness")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install research harness" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "research"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]


def test_spaces_ui_install_dashboard_demo_posts_template_and_refreshes_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "installDashboardDemo")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install dashboard demo" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "dashboard"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_kanban_board_posts_template_and_refreshes_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "installKanbanBoard")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install kanban board" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "kanban"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_notes_app_posts_template_and_refreshes_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "installNotesApp")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install notes app" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "notes"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_browser_surface_posts_template_and_refreshes_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "installBrowserSurface")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install browser surface" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "browser"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_stock_chart_posts_template_and_refreshes_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "installStockChart")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install stock chart" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "stock"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_camera_dashboard_posts_template_and_refreshes_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "installCameraDashboard")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install camera dashboard" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "camera"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
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


def test_spaces_ui_install_game_sandbox_posts_template_and_refreshes_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "installGameSandbox")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install game sandbox" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "game"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_music_sequencer_posts_template_and_refreshes_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "installMusicSequencer")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install music sequencer" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "music"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_local_service_dashboard_posts_template_and_refreshes_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "installLocalServiceDashboard")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install local service dashboard" in out["rootHtml"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"template": "service"}
    assert out["calls"][-1]["path"] == "api/spaces"
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


def test_spaces_ui_install_model_setup_posts_template_and_refreshes_without_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "installModelSetup")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/templates/install")

    assert "Install model setup" in out["rootHtml"]
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
    assert "2 / 2 metadata-only smokes passed" in out["rootHtml"]
    assert "demo_weather_widget" in out["rootHtml"]
    assert "demo_time_travel_restore" in out["rootHtml"]
    assert "persistence: checked" in out["rootHtml"]
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
    assert "Disable widget" in out["recoveryHtml"]
    assert "Enable widget" in out["recoveryHtml"]
    assert "Ask Capy to repair" in out["recoveryHtml"]
    assert "Disabled: render failed" in out["recoveryHtml"]
    assert "Generated widgets rendered: false" in out["recoveryHtml"]
    assert "<script>" not in out["recoveryHtml"]
    assert "renderer" not in out["recoveryHtml"]


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
    assert "name: Lab &lt;Detail&gt;" in out["rootHtml"]
    assert 'data-capy-action="restoreRevision"' in out["rootHtml"]
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
    assert "U0VDUkVUX1pJUF9JTUFHSU5BUlk" not in out["rootHtml"]
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
    assert "space_yaml" not in out["rootHtml"]
    assert "widgets/weather.yaml" not in out["rootHtml"]
    assert "api_key" not in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
    assert "SECRET" not in out["rootHtml"]


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
