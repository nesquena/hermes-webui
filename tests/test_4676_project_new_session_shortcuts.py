"""Regression coverage for #4676: project-scope quick conversation creation."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
STYLE_CSS = ROOT / "static" / "style.css"
NODE = shutil.which("node")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.find(marker)
    assert start >= 0, f"{name} function not found in static/sessions.js"
    brace = source.find("{", start)
    assert brace >= 0, f"{name} declaration has no opening brace"
    depth = 0
    for idx in range(brace, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"{name} function body not closed")


def test_new_session_uses_explicit_project_override_before_active_filter():
    src = _read(SESSIONS_JS)
    assert "Object.prototype.hasOwnProperty.call(options,'project_id')" in src
    assert "reqBody.project_id=options.project_id" in src


def test_quick_create_button_attaches_filter_align_and_request_path():
    src = _read(SESSIONS_JS)
    helper = _extract_function(src, "_attachProjectQuickCreateButton")
    assert "project-chip-quick-create" in helper
    assert "_setActiveProjectFilter(project.project_id)" in helper
    assert "newSession(false,{project_id:project.project_id})" in helper
    assert "if(_newSessionInFlight)" in helper
    assert "_setActiveProjectFilter(previousProject)" in helper
    assert "btn.ondblclick" in helper
    assert "btn.oncontextmenu" in helper
    assert "btn.ontouchstart" in helper
    assert "btn.ontouchend" in helper


def test_quick_create_button_render_is_gated_off_by_default():
    """#4676 quick-create buttons must be opt-in: the chip render site only
    attaches the per-project '+' button when window._projectQuickCreate is set."""
    src = _read(SESSIONS_JS)
    assert "if(window._projectQuickCreate) _attachProjectQuickCreateButton(chip,p);" in src
    # The attach call must never run unconditionally at the render site.
    assert "\n      _attachProjectQuickCreateButton(chip,p);" not in src


def test_project_quick_create_styles_exist_and_are_discrete_to_pointer_layouts():
    css = _read(STYLE_CSS)
    assert ".project-chip-quick-create" in css
    assert ".project-chip:hover .project-chip-quick-create" in css
    assert ".project-chip:focus-within .project-chip-quick-create" in css
    assert ".project-chip-quick-create:hover" in css
    assert "@media (hover:none) and (pointer:coarse)" in css


def _run_new_session_case(options, active_project=None):
    _DRIVER = r"""
const fs = require('fs');
const [path, argsJson] = process.argv.slice(-2);
const args = JSON.parse(argsJson);
const src = fs.readFileSync(path, 'utf8');

function extractAsyncFunction(source, name) {
  const marker = `async function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(name + ' not found');
  const brace = source.indexOf('{', source.indexOf(')', start));
  let depth = 0;
  for (let i = brace; i < source.length; i++) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('function body not closed for ' + name);
}

const newSessionSrc = extractAsyncFunction(src, 'newSession');

globalThis.window = globalThis;
globalThis.document = {
  baseURI: 'http://example.test/',
  createElement(tag) {
    const node = {
      tagName: String(tag || '').toUpperCase(),
      children: [],
      appendChild(child) { this.children.push(child); },
      textContent: '',
      value: '',
      selectedOptions: [{ dataset: { provider: '' } }],
      dataset: {},
    };
    return node;
  },
};
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.history = { replaceState: () => {} };
globalThis.NO_PROJECT_FILTER = '__none__';
globalThis._activeProject = args.activeProject;
globalThis._sessionSourceFilter = 'webui';
globalThis._newSessionInFlight = null;
globalThis._messagesTruncated = false;
globalThis._oldestIdx = 0;
globalThis.INFLIGHT = {};
globalThis.S = {
  session: args.session || null,
  toolCalls: [],
  messages: [],
  activeProfile: 'default',
  _pendingSessionToolsets: null,
  _profileSwitchWorkspace: null,
  _profileDefaultWorkspace: null,
};
globalThis._defaultModel = null;
globalThis._activeProvider = 'openai';
globalThis._emptyComposerModelOverride = null;
globalThis._readPersistedModelState = () => null;
globalThis._readEmptyComposerModelOverride = () => null;
globalThis._clearEmptyComposerModelOverride = () => {};
globalThis.$ = (id) => (id === 'modelSelect' ? { value: 'gpt-4', selectedOptions: [{ dataset: { provider: 'openai' } }] } : null);
for (const name of [
  '_setNewSessionPending', 'updateQueueBadge', '_clearPendingSelections',
  'clearLiveToolCards', 'setComposerStatus', 'setStatus', 'updateSendBtn',
  'syncTopbar', 'renderMessages', 'startSessionStream', '_setSessionViewedCount',
  '_setActiveSessionUrl', '_rememberNewChatDraftSession', '_hydrateTodosFromSession',
  '_setLiveAssistantTps', '_syncCtxIndicator', 'showToast'
]) {
  globalThis[name] = () => {};
}
globalThis.loadDir = async () => null;
globalThis._applyModelToDropdown = () => true;
globalThis._modelStateForSelect = () => ({ model: 'gpt-4', model_provider: 'openai' });
globalThis._readPersistedModelState = () => null;
globalThis.getModelLabel = (v) => v || '';
globalThis._defaultModel = null;

const calls = [];
globalThis.api = async (_url, opts) => {
  calls.push(JSON.parse(opts.body));
  return { session: { session_id: 's-1', messages: [], model: 'gpt-4', model_provider: 'openai', workspace: null, message_count: 0, last_usage: {} } };
};

eval(newSessionSrc);

(async () => {
  await newSession(false, args.options);
  console.log(JSON.stringify({ body: calls[0] || {} }));
})().catch(err => {
  console.error(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
"""

    payload = {
        "activeProject": active_project,
        "options": options,
        "session": {"session_id": "session-1"},
    }
    result = subprocess.run(
        [NODE, "-e", _DRIVER, str(SESSIONS_JS), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])["body"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_aligns_project_id_override_when_explicitly_set():
    body = _run_new_session_case({"project_id": "explicit-project"}, active_project="active-project")
    assert body["project_id"] == "explicit-project"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_respects_explicit_project_id_none():
    body = _run_new_session_case({"project_id": None}, active_project="active-project")
    assert "project_id" in body
    assert body["project_id"] is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_falls_back_to_active_project_when_override_missing():
    body = _run_new_session_case({}, active_project="active-project")
    assert body["project_id"] == "active-project"


_HELPER = r"""
const fs = require('fs');
const [sessionsPath, paramsJson] = process.argv.slice(-2);
const sessionsSrc = fs.readFileSync(sessionsPath, 'utf8');
const params = JSON.parse(paramsJson);

function extractFunction(source, name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(name + ' not found');
  const brace = source.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('function body not closed for ' + name);
}

globalThis.window = globalThis;
globalThis.document = {
  createElement(tag) {
    return {
      tagName: String(tag || '').toUpperCase(),
      className: '',
      textContent: '',
      children: [],
      appendChild(child) { this.children.push(child); },
      appendChildCallCount: 0,
      attributes: {},
      setAttribute(name, value) { this.attributes[name] = String(value); },
      getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; },
      dataset: {},
      type: '',
    };
  },
};
globalThis._setActiveProjectFilter = (projectId) => {
  globalThis._activeProject = projectId;
  params.filterProjectId = projectId;
  params.calls.push({type: 'set-filter', projectId});
};
globalThis._activeProject = params.activeProject;
globalThis.newSession = async (flash, options) => {
  if (globalThis._newSessionInFlight) {
    params.toasts.push('New conversation already in progress');
    return globalThis._newSessionInFlight;
  }
  if (params.failNewSession) throw new Error(params.failMessage || 'request failed');
  params.newSession = {flash, options};
  params.calls.push({type: 'new-session', flash, options});
  return params.newSessionResult || { session_id: 's-1' };
};
globalThis.showToast = (message) => {
  params.toasts.push(String(message || ''));
};
globalThis._newSessionInFlight = params.newSessionInFlightReject
  ? Promise.reject(new Error(params.newSessionInFlightReject))
  : (params.newSessionInFlight
      ? Promise.resolve(params.newSessionInFlight)
      : null);

eval(extractFunction(sessionsSrc, '_attachProjectQuickCreateButton'));

const chip = {
  appended: [],
  appendChild(child) { this.appended.push(child); },
};
_attachProjectQuickCreateButton(chip, { project_id: params.projectId });
const btn = chip.appended[0];
const ev = {
  stopPropagation() { params.stopCount++; },
  preventDefault() { params.preventCount++; },
  stopImmediatePropagation() { params.stopImmediateCount++; },
};
const touchEv = {
  stopPropagation() { params.touchStopCount++; },
  preventDefault() { params.touchPreventCount++; },
  stopImmediatePropagation() { params.touchStopImmediateCount++; },
};
(async () => {
  await btn.onclick(ev);
  btn.ondblclick(ev);
  btn.oncontextmenu(ev);
  btn.ontouchstart(touchEv);
  btn.ontouchend(touchEv);
  console.log(JSON.stringify({
    buttonClass: btn.className,
    buttonTag: btn.tagName,
    buttonText: btn.textContent,
    buttonAriaLabel: btn.getAttribute('aria-label'),
    newSession: params.newSession,
    filterProjectId: params.filterProjectId,
    stopCount: params.stopCount,
    preventCount: params.preventCount,
    stopImmediateCount: params.stopImmediateCount,
    touchStopCount: params.touchStopCount,
    touchPreventCount: params.touchPreventCount,
    touchStopImmediateCount: params.touchStopImmediateCount,
    calls: params.calls,
    toasts: params.toasts,
  }));
})().catch(err => {
  console.error(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
"""


def _run_quick_create_case(
    project_id="example-project",
    *,
    active_project="active-project",
    fail_new_session=False,
    new_session_inflight=None,
    new_session_inflight_reject=None,
):
    payload = {
        "projectId": project_id,
        "activeProject": active_project,
        "filterProjectId": active_project,
        "calls": [],
        "stopCount": 0,
        "preventCount": 0,
        "stopImmediateCount": 0,
        "touchStopCount": 0,
        "touchPreventCount": 0,
        "touchStopImmediateCount": 0,
        "failNewSession": fail_new_session,
        "newSessionInFlight": new_session_inflight,
        "newSessionInFlightReject": new_session_inflight_reject,
        "toasts": [],
    }
    result = subprocess.run(
        [NODE, "-e", _HELPER, str(SESSIONS_JS), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"node helper failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_project_chip_quick_create_keeps_active_filter_and_uses_project_override():
    out = _run_quick_create_case("project-123")
    assert out["buttonClass"] == "project-chip-quick-create"
    assert out["buttonTag"] == "BUTTON"
    assert out["buttonText"] == "+"
    assert out["buttonAriaLabel"] == "New conversation in this project"
    assert out["filterProjectId"] == "project-123"
    assert out["newSession"] == {"flash": False, "options": {"project_id": "project-123"}}
    assert {"type": "set-filter", "projectId": "project-123"} in out["calls"]
    assert {"type": "new-session", "flash": False, "options": {"project_id": "project-123"}} in out["calls"]
    assert out["stopCount"] >= 3
    assert out["preventCount"] >= 3
    assert out["stopImmediateCount"] >= 3
    assert out["touchStopCount"] >= 2
    assert out["touchPreventCount"] == 0
    assert out["touchStopImmediateCount"] >= 2


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_project_chip_quick_create_restores_filter_when_new_session_fails():
    out = _run_quick_create_case(
        "project-123",
        active_project="keep-me",
        fail_new_session=True,
    )

    assert out["filterProjectId"] == "keep-me"
    assert {"type": "set-filter", "projectId": "project-123"} in out["calls"]
    assert {"type": "set-filter", "projectId": "keep-me"} in out["calls"]
    assert any(msg.startswith("New conversation failed:") for msg in out["toasts"])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_project_chip_quick_create_leaves_filter_unchanged_during_inflight_guard():
    out = _run_quick_create_case(
        "project-123",
        active_project="keep-me",
        new_session_inflight={"session_id": "existing"},
    )

    assert out["filterProjectId"] == "keep-me"
    assert {"type": "set-filter", "projectId": "project-123"} not in out["calls"]
    assert "newSession" not in out


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_project_chip_quick_create_swallows_duplicate_inflight_rejections():
    out = _run_quick_create_case(
        "project-123",
        active_project="keep-me",
        new_session_inflight_reject="request failed",
    )

    assert out["filterProjectId"] == "keep-me"
    assert {"type": "set-filter", "projectId": "project-123"} not in out["calls"]
    assert out["toasts"] == ["New conversation already in progress"]
