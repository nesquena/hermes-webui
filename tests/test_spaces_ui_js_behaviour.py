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
    return response({ enabled: true, spaces: [{ space_id: 'lab', name: 'Lab', widget_count: 1, revision_event_id: 'rev1' }] });
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
          renderer: '<script>bad()</script>',
          widgets: [
            { id: 'bad-widget', kind: 'html', title: 'Bad <Widget>', disabled: false, renderer: '<script>bad()</script>' },
            { id: 'disabled-widget', kind: 'markdown', title: 'Disabled Widget', disabled: true, disabled_reason: 'render failed' },
          ],
        }
      ],
    });
  }
  if (path === 'api/spaces/widgets?space_id=lab') {
    return response({ widgets: [{ id: 'weather', kind: 'markdown', title: '<Weather>', layout: { x: 12, y: 3, w: 5, h: 4, minimized: false }, renderer: '<script>bad()</script>' }] });
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
    } });
  }
  if (path === 'api/spaces/revisions?space_id=lab') {
    return response({ revisions: [
      { event_id: 'rev2', event_type: 'widget.updated', space_id: 'lab', created_at: 1710000000, details: { widget_id: 'weather', renderer: '<script>bad()</script>' } },
      { event_id: 'rev1', event_type: 'space.created', space_id: 'lab', created_at: 1709999900, details: { name: 'Lab <Detail>' } },
    ] });
  }
  if (path === 'api/spaces/widget/upsert') {
    return response({ space_id: 'lab', widget: { id: 'notes', kind: 'markdown', title: 'Notes', layout: { x: 2, y: 3, w: 8, h: 5 } }, revision_event_id: 'rev2' });
  }
  if (path === 'api/spaces/widget/patch') {
    return response({ space_id: 'lab', widget: { id: 'weather', kind: 'markdown', title: 'Weather patched', layout: { x: 4, y: 5, w: 9, h: 6 } }, revision_event_id: 'rev-patch', renderer: '<script>bad()</script>' });
  }
  if (path === 'api/spaces/widget/delete') {
    return response({ deleted: true, space_id: 'lab', widget_id: 'weather', revision_event_id: 'rev3' });
  }
  if (path === 'api/spaces/widget/event') {
    return response({ queued: true, space_id: 'lab', widget_id: 'weather', event_name: 'agent.prompt', event_id: 'evt1' });
  }
  if (path === 'api/spaces/recovery/disable-widget') {
    return response({ disabled: true, space_id: 'broken', widget_id: 'bad-widget', revision_event_id: 'rev-disable' });
  }
  if (path === 'api/spaces/create') {
    return response({ space: { space_id: 'ops', name: 'Ops', description: '<b>Operations</b>', widget_count: 0, revision_event_id: 'rev4' } });
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
    return response({
      template: 'weather',
      space: { space_id: 'weather-demo', name: 'Weather Demo', description: 'Prague weather starter', widget_count: 1, revision_event_id: 'rev-weather' },
      installed_widgets: [{ id: 'weather-current', kind: 'weather', title: 'Weather in Prague', layout: { x: 0, y: 0, w: 8, h: 5, minimized: false }, renderer: '<script>bad()</script>' }],
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
    return response({
      ok: true,
      source: 'space-agent-yaml',
      space: { space_id: 'imported-lab', name: 'Imported Lab', description: 'Imported safely', widget_count: 1, revision_event_id: 'rev-import' },
      imported_widgets: [{ id: 'weather', kind: 'html', title: 'Weather', renderer: '<script>bad()</script>', api_key: 'SECRET' }],
      space_yaml: body.space_yaml,
      widgets: body.widgets,
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
  } else if (scenario === 'editWidget') {
    if (typeof window.loadSpaceWidgets !== 'function') throw new Error('loadSpaceWidgets missing');
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('editWidget', { spaceId: 'lab', widgetId: 'weather', widgetTitle: '<Weather>', widgetKind: 'markdown', widgetX: '12', widgetY: '3', widgetW: '5', widgetH: '4' });
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
  } else if (scenario === 'askWidget') {
    global.showPromptDialog = async function(opts) { dialogs.push(opts); return 'Refresh the weather widget'; };
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('askWidget', { spaceId: 'lab', widgetId: 'weather', widgetTitle: '<Weather>' });
  } else if (scenario === 'askWidgetNoPrompt') {
    await window.loadCapySpaces();
    await window.loadSpaceWidgets('lab');
    await click('askWidget', { spaceId: 'lab', widgetId: 'weather', widgetTitle: '<Weather>' });
  } else if (scenario === 'createSpace') {
    await window.loadCapySpaces();
    await click('saveSpace', {});
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
  } else if (scenario === 'installBigBangOnboarding') {
    await window.loadCapySpaces();
    await click('installBigBangTemplate', {});
  } else if (scenario === 'openSpaceDetail') {
    await window.loadCapySpaces();
    await click('openSpace', { spaceId: 'lab' });
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
  } else if (scenario === 'activateSpace') {
    await window.loadCapySpaces();
    await click('activateSpace', { spaceId: 'lab' });
  } else if (scenario === 'systemWidgetShell') {
    await window.loadCapySpaces();
    await click('openSystemPanel', { systemPanel: 'chat' });
    await click('openSystemPanel', { systemPanel: 'settings' });
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
  } else {
    throw new Error('unknown scenario: ' + scenario);
  }
  process.stdout.write(JSON.stringify({ rootHtml: root.innerHTML, recoveryHtml: makeElement('capySpacesRecovery').innerHTML, recoveryText: makeElement('capySpacesRecovery').textContent, calls, values, rootDataset: root.dataset, dialogs, switchedPanels, capySpaceSyncs }));
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


def test_spaces_ui_delete_widget_posts_to_delete_and_refreshes_widgets(driver_path):
    out = _run_spaces_scenario(driver_path, "delete")
    post = next(call for call in out["calls"] if call["path"] == "api/spaces/widget/delete")

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

    assert "Use in chat" in out["rootHtml"]
    assert "Active in chat" in out["rootHtml"]
    assert out["capySpaceSyncs"] == ["lab"]
    assert post["method"] == "POST"
    assert json.loads(post["body"]) == {"space_id": "lab", "session_id": "session-123"}
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
    assert "Disable widget" in out["recoveryHtml"]
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
    assert "Revision history" in out["rootHtml"]
    assert "widget.updated" in out["rootHtml"]
    assert "space.created" in out["rootHtml"]
    assert "rev2" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]


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
