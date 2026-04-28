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
global.document = {
  getElementById: makeElement,
};
global.fetch = async function(path, opts = {}) {
  calls.push({ path, method: opts.method || 'GET', body: opts.body || '' });
  if (path === 'api/spaces') {
    return response({ enabled: true, spaces: [{ space_id: 'lab', name: 'Lab', widget_count: 1, revision_event_id: 'rev1' }] });
  }
  if (path === 'api/spaces/recovery') {
    return response({ enabled: true, generated_widgets_rendered: false });
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
  if (path === 'api/spaces/widget/upsert') {
    return response({ space_id: 'lab', widget: { id: 'notes', kind: 'markdown', title: 'Notes', layout: { x: 2, y: 3, w: 8, h: 5 } }, revision_event_id: 'rev2' });
  }
  if (path === 'api/spaces/widget/delete') {
    return response({ deleted: true, space_id: 'lab', widget_id: 'weather', revision_event_id: 'rev3' });
  }
  if (path === 'api/spaces/widget/event') {
    return response({ queued: true, space_id: 'lab', widget_id: 'weather', event_name: 'agent.prompt', event_id: 'evt1' });
  }
  if (path === 'api/spaces/create') {
    return response({ space: { space_id: 'ops', name: 'Ops', description: '<b>Operations</b>', widget_count: 0, revision_event_id: 'rev4' } });
  }
  if (path === 'api/spaces/update') {
    return response({ space: { space_id: 'lab', name: 'Lab Edited', description: 'Updated', widget_count: 1, revision_event_id: 'rev5' } });
  }
  if (path === 'api/spaces/delete') {
    return response({ deleted: true, space_id: 'lab', revision_event_id: 'rev6' });
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
  } else if (scenario === 'openSpaceDetail') {
    await window.loadCapySpaces();
    await click('openSpace', { spaceId: 'lab' });
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
  } else {
    throw new Error('unknown scenario: ' + scenario);
  }
  process.stdout.write(JSON.stringify({ rootHtml: root.innerHTML, calls, values, rootDataset: root.dataset, dialogs }));
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
    assert out["calls"][-1]["path"] == "api/spaces/widgets?space_id=lab"


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


def test_spaces_ui_delete_space_fails_closed_without_shared_dialog(driver_path):
    out = _run_spaces_scenario(driver_path, "deleteSpace")

    assert not any(call["path"] == "api/spaces/delete" for call in out["calls"])


def test_spaces_ui_cancelled_delete_space_does_not_post(driver_path):
    out = _run_spaces_scenario(driver_path, "deleteSpaceCancelled")

    assert out["dialogs"]
    assert not any(call["path"] == "api/spaces/delete" for call in out["calls"])


def test_spaces_ui_opens_space_detail_without_rendering_widget_code(driver_path):
    out = _run_spaces_scenario(driver_path, "openSpaceDetail")

    assert {"path": "api/spaces/get?space_id=lab", "method": "GET", "body": ""} in out["calls"]
    assert "Lab &lt;Detail&gt;" in out["rootHtml"]
    assert "Unsafe &lt;detail&gt;" in out["rootHtml"]
    assert "&lt;Weather&gt;" in out["rootHtml"]
    assert "x12 y3 · 5×4" in out["rootHtml"]
    assert "<script>" not in out["rootHtml"]
    assert "renderer" not in out["rootHtml"]
