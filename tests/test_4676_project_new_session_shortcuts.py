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
    # Bindings feature: the + button forwards the project's pinned context via
    # Object.assign so an unbound project behaves exactly like the legacy
    # `newSession(false,{project_id:...})` call.
    assert "newSession(false,Object.assign({project_id:project.project_id},bindings))" in helper
    assert "_projectBindingsForNewSession(project)" in helper
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


def _run_new_session_case(options, active_project=None, all_projects=None):
    if all_projects is None:
        all_projects = []
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

function extractFunction(source, name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(name + ' not found');
  const brace = source.indexOf('{', start);
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
const bindingsSrc = extractFunction(src, '_projectBindingsForNewSession');
eval(bindingsSrc);

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
globalThis._allProjects = args.allProjects || [];
globalThis._sessionSourceFilter = 'webui';
globalThis._newSessionInFlight = null;
// #8356: newSession() now clears _loadingSessionId and advances
// _loadSessionGeneration when it takes the pane; declare both here so the
// extracted source runs in this harness.
globalThis._loadingSessionId = null;
globalThis._loadSessionGeneration = 0;
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
        "allProjects": all_projects,
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


_BOUND_PROJECTS = [
    {
        "project_id": "proj-alpha",
        "workspaces": ["D:/alpha"],
        "default_workspace": "D:/alpha",
        "model": "alpha-model",
        "model_provider": "alpha-prov",
        "reasoning_effort": "high",
    },
    {
        "project_id": "proj-beta",
        "workspaces": ["D:/beta"],
        "default_workspace": "D:/beta",
        "model": "beta-model",
        "model_provider": "beta-prov",
        "reasoning_effort": "medium",
    },
]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_applies_active_project_bindings_from_chat_plus():
    """The chat-header/rail '+' (btnNewChat) with an active project filter must
    apply that project's bindings (default workspace / model / effort) exactly
    like the project-chip quick-create '+': selecting a project then pressing
    the top-level New Chat button opens the session in the project's pinned
    workspace.
    """
    body = _run_new_session_case(
        {},
        active_project="proj-alpha",
        all_projects=_BOUND_PROJECTS,
    )
    assert body["project_id"] == "proj-alpha"
    assert body["workspace"] == "D:/alpha"
    assert body["model"] == "alpha-model"
    assert body["model_provider"] == "alpha-prov"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_switch_project_reapplies_new_bindings():
    """User scenario: create a session (project A active) → switch to project B
    → press '+' again. The second session must pick up project B's bindings,
    not stale A's settings.
    """
    body_a = _run_new_session_case(
        {},
        active_project="proj-alpha",
        all_projects=_BOUND_PROJECTS,
    )
    assert body_a["project_id"] == "proj-alpha"
    assert body_a["workspace"] == "D:/alpha"
    assert body_a["model"] == "alpha-model"

    body_b = _run_new_session_case(
        {},
        active_project="proj-beta",
        all_projects=_BOUND_PROJECTS,
    )
    assert body_b["project_id"] == "proj-beta"
    assert body_b["workspace"] == "D:/beta"
    assert body_b["model"] == "beta-model"
    assert body_b["model_provider"] == "beta-prov"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_explicit_options_beat_active_project_bindings():
    """An explicit workspace/model passed by the caller (e.g. chip '+', which
    passes bindings itself) must NOT be overwritten by the active project's
    bindings merge.
    """
    body = _run_new_session_case(
        {"project_id": "proj-alpha", "workspace": "D:/explicit", "model": "explicit-model"},
        active_project="proj-alpha",
        all_projects=_BOUND_PROJECTS,
    )
    assert body["project_id"] == "proj-alpha"
    assert body["workspace"] == "D:/explicit"
    assert body["model"] == "explicit-model"


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

// The + button forwards the project's pinned bindings (workspace/model)
// alongside project_id. A bare {project_id} project yields no bindings,
// matching the pre-bindings behavior. reasoning_effort is deliberately NOT
// forwarded (UX re-gate 2026-10-08T23:39:33Z, item A).
globalThis._projectBindingsForNewSession = (project) => {
  const o = {};
  if (project) {
    if (project.workspace) o.workspace = project.workspace;
    if (project.model) o.model = project.model;
    if (project.model_provider) o.model_provider = project.model_provider;
  }
  return o;
};

eval(extractFunction(sessionsSrc, '_attachProjectQuickCreateButton'));
eval(extractFunction(sessionsSrc, '_projectBindingsForNewSession'));

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


# ── #7824 review P1: the middle-click new-chat link must carry the selected
# project, and the new tab must apply it before creating the conversation ──

BOOT_JS = ROOT / "static" / "boot.js"

_NEW_CHAT_LINK_DRIVER = r"""
const fs = require('fs');
const [path, argsJson] = process.argv.slice(-2);
const args = JSON.parse(argsJson);
const src = fs.readFileSync(path, 'utf8');

function extractFunction(source, name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(name + ' not found');
  const brace = source.indexOf('{', start);
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

let replacedUrl = null;
const anchor = {
  tagName: 'A',
  attrs: { href: args.initialHref || './?action=new-chat' },
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; },
  setAttribute(name, value) { this.attrs[name] = String(value); },
};

globalThis.window = {
  location: { href: args.locationHref },
  history: { state: null, replaceState(_state, _title, url) { replacedUrl = url; } },
};
globalThis.document = { baseURI: 'http://example.test/' };
globalThis.$ = (id) => (id === 'btnNewChat' ? anchor : null);
globalThis.NO_PROJECT_FILTER = '__none__';
globalThis.S = { activeProfile: args.activeProfile || 'default' };
globalThis._allProjects = args.allProjects || [];
globalThis._profileMatchesActiveProfile = (a, b) => (a || 'default') === (b || 'default');
globalThis._activeProject = Object.prototype.hasOwnProperty.call(args, 'activeProject') ? args.activeProject : null;

eval(extractFunction(src, '_newChatLinkHref'));
eval(extractFunction(src, '_syncNewChatLinkHref'));
eval(extractFunction(src, '_resolveNewChatProjectIntent'));
eval(extractFunction(src, '_consumeNewChatProjectParamFromLocation'));

let consumed = null;
if (args.consume) consumed = _consumeNewChatProjectParamFromLocation();
console.log(JSON.stringify({
  href: _newChatLinkHref(),
  anchorHref: anchor.attrs.href,
  activeProject: globalThis._activeProject,
  consumed: consumed,
  replacedUrl: replacedUrl,
}));
"""


def _run_new_chat_link_case(
    active_project=None,
    location_href="http://example.test/",
    consume=False,
    all_projects=None,
    active_profile="default",
):
    assert NODE is not None
    payload = {
        "activeProject": active_project,
        "locationHref": location_href,
        "consume": consume,
        "allProjects": all_projects or [],
        "activeProfile": active_profile,
    }
    result = subprocess.run(
        [NODE, "-e", _NEW_CHAT_LINK_DRIVER, str(SESSIONS_JS), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


_PROJECT_ALPHA = [{"project_id": "proj-alpha", "profile": "default"}]
_PROJECT_FOREIGN = [{"project_id": "proj-alpha", "profile": "work"}]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_chat_link_href_carries_active_project():
    out = _run_new_chat_link_case(active_project="proj-alpha")
    assert out["href"] == "./?action=new-chat&project=proj-alpha"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_chat_link_href_stays_plain_without_project_filter():
    out = _run_new_chat_link_case(active_project=None)
    assert out["href"] == "./?action=new-chat"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_chat_link_href_carries_unassigned_filter():
    """The sidebar's "No project" view is a real filter too: a new tab must not
    silently auto-assign the conversation to some project."""
    out = _run_new_chat_link_case(active_project="__none__")
    assert out["href"] == "./?action=new-chat&project=__none__"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_chat_project_param_applies_project_before_creation():
    out = _run_new_chat_link_case(
        location_href="http://example.test/?action=new-chat&project=proj-alpha",
        consume=True,
        all_projects=_PROJECT_ALPHA,
    )
    assert out["consumed"] == "proj-alpha"
    assert out["activeProject"] == "proj-alpha"
    # One-shot: the param must not survive into the session URL.
    assert out["replacedUrl"] == "/?action=new-chat"
    # The new tab's own "+" keeps carrying the project for the next gesture.
    assert out["anchorHref"] == "./?action=new-chat&project=proj-alpha"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_chat_project_param_ignores_unknown_project():
    out = _run_new_chat_link_case(
        location_href="http://example.test/?action=new-chat&project=ghost",
        consume=True,
        all_projects=_PROJECT_ALPHA,
    )
    assert out["consumed"] is None
    assert out["activeProject"] is None
    assert out["replacedUrl"] == "/?action=new-chat"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_chat_project_param_rejects_foreign_profile_project():
    out = _run_new_chat_link_case(
        location_href="http://example.test/?action=new-chat&project=proj-alpha",
        consume=True,
        all_projects=_PROJECT_FOREIGN,
        active_profile="default",
    )
    assert out["consumed"] is None
    assert out["activeProject"] is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_chat_project_param_needs_the_new_chat_action():
    """A bare ?project= URL is not ours: leave it alone instead of silently
    turning it into a sidebar filter."""
    out = _run_new_chat_link_case(
        location_href="http://example.test/?project=proj-alpha",
        consume=True,
        all_projects=_PROJECT_ALPHA,
    )
    assert out["consumed"] is None
    assert out["activeProject"] is None
    assert out["replacedUrl"] is None


def test_project_filter_changes_sync_the_new_chat_link():
    src = _read(SESSIONS_JS)
    filter_body = _extract_function(src, "_setActiveProjectFilter")
    assert "_syncNewChatLinkHref()" in filter_body
    source_body = _extract_function(src, "_setSessionSourceFilter")
    assert "_syncNewChatLinkHref()" in source_body


def test_boot_applies_new_chat_project_before_creating_the_conversation():
    boot = _read(BOOT_JS)
    consume_at = boot.find("_consumeNewChatProjectParamFromLocation()")
    launch_at = boot.find("if(_shouldStartFreshPwaChat(pwaLaunchAction,urlSession))")
    create_at = boot.find("await newSession(true)", launch_at if launch_at >= 0 else 0)
    assert consume_at >= 0, "boot must consume the new-chat project param"
    assert launch_at >= 0, "boot new-chat launch branch not found"
    assert create_at >= 0, "boot must create the launched conversation"
    assert consume_at < launch_at < create_at, (
        "the project intent must be applied before the new chat is created"
    )

