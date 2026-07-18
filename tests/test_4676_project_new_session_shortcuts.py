"""Regression coverage for #4676: project-scope quick conversation creation."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
PANELS_JS = ROOT / "static" / "panels.js"
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


def _run_new_session_case(
    options,
    active_project=None,
    all_projects=None,
    profile_default_workspace=None,
    switch_workspace=None,
    switch_response_workspace=None,
    session=None,
    messages=None,
    active_profile="default",
    show_all_profiles=False,
    timeout_ms=250,
    return_meta=False,
):
    _DRIVER = r"""
const fs = require('fs');
const [sessionsPath, panelsPath, argsJson] = process.argv.slice(-3);
const args = JSON.parse(argsJson);
const sessionsSrc = fs.readFileSync(sessionsPath, 'utf8');
const panelsSrc = fs.readFileSync(panelsPath, 'utf8');

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

const profileMatchSrc = extractFunction(sessionsSrc, '_profileMatchesActiveProfile');
const resolverSrc = extractFunction(sessionsSrc, '_resolveProjectForNewSession');
const ensureProjectProfileSrc = extractAsyncFunction(sessionsSrc, '_ensureProjectProfileForNewSession');
const newSessionSrc = extractAsyncFunction(sessionsSrc, 'newSession');
const switchToProfileSrc = extractAsyncFunction(panelsSrc, 'switchToProfile');

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
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
globalThis.history = { replaceState: () => {} };
globalThis.NO_PROJECT_FILTER = '__none__';
globalThis._activeProject = args.activeProject;
globalThis._allProjects = args.allProjects || [];
globalThis._showAllProfiles = !!args.showAllProfiles;
globalThis._profileSwitchCallerOwnsNewSession = false;
globalThis._profileSwitchOpeningExistingSession = false;
globalThis._profileSwitchGeneration = 0;
globalThis._workspacePanelMode = 'closed';
globalThis._renamingSid = null;
globalThis._sessionSourceFilter = 'webui';
globalThis._newSessionInFlight = null;
globalThis._messagesTruncated = false;
globalThis._oldestIdx = 0;
globalThis.INFLIGHT = {};
globalThis._skillsData = null;
globalThis._workspaceList = null;
globalThis.S = {
  session: args.session || null,
  toolCalls: [],
  messages: args.messages || [],
  activeProfile: args.activeProfile || 'default',
  activeProfileIsDefault: !args.activeProfile || args.activeProfile === 'default',
  _pendingSessionToolsets: null,
  _profileSwitchWorkspace: args.switchWorkspace !== undefined ? args.switchWorkspace : null,
  _profileDefaultWorkspace: args.profileDefaultWorkspace !== undefined ? args.profileDefaultWorkspace : null,
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
  '_setLiveAssistantTps', '_syncCtxIndicator', 'showToast', 'closeSessionActionMenu',
  '_invalidateSessionListRenders', '_setProfileSwitchListEmbargo', 'showSessionListSkeleton',
  'bumpWorkspaceTreeGen', 'showWorkspaceTreeSkeleton', 'startGatewaySSE', 'applyBotName',
  '_clearPersistedModelState', 'animateNextSessionListRefresh', '_openProfileSwitchSessionBrowser',
  'clearWorkspaceTreeSkeleton', '_profileSwitchPanelLoad', '_refreshProfileSwitchBackground'
]) {
  globalThis[name] = () => {};
}
globalThis.loadDir = async () => null;
globalThis.renderSessionList = async () => null;
globalThis._applyModelToDropdown = () => true;
globalThis._modelStateForSelect = () => ({ model: 'gpt-4', model_provider: 'openai' });
globalThis._readPersistedModelState = () => null;
globalThis.getModelLabel = (v) => v || '';
globalThis._defaultModel = null;
globalThis.t = (key, value) => value ? `${key}:${value}` : String(key || '');

const calls = [];
const switchCalls = [];
let sessionNewCalls = 0;
globalThis.api = async (url, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : {};
  if (url === '/api/profile/switch') {
    switchCalls.push(String(body.name || ''));
    return {
      active: body.name || 'default',
      is_default: !body.name || body.name === 'default',
      default_workspace: args.switchResponseWorkspace !== undefined ? args.switchResponseWorkspace : (args.profileDefaultWorkspace || null),
      default_model: null,
      default_model_provider: null,
    };
  }
  if (url === '/api/session/new') {
    sessionNewCalls += 1;
    calls.push(body);
    return { session: { session_id: `s-${sessionNewCalls}`, messages: [], model: 'gpt-4', model_provider: 'openai', workspace: body.workspace || null, message_count: 0, last_usage: {} } };
  }
  if (url === '/api/session/update') {
    return { ok: true };
  }
  throw new Error('unexpected api url: ' + url);
};

eval(profileMatchSrc);
eval(resolverSrc);
eval(ensureProjectProfileSrc);
eval(newSessionSrc);
eval(switchToProfileSrc);

(async () => {
  const result = await Promise.race([
    newSession(false, args.options).then(() => ({ timedOut: false })),
    new Promise((resolve) => setTimeout(() => resolve({ timedOut: true }), args.timeoutMs || 250)),
  ]);
  console.log(JSON.stringify({
    timedOut: !!result.timedOut,
    body: calls[0] || {},
    switchCalls,
    activeProfile: globalThis.S.activeProfile,
    callCount: sessionNewCalls,
  }));
})().catch(err => {
  console.error(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
"""

    payload = {
        "activeProject": active_project,
        "allProjects": all_projects or [],
        "options": options,
        "profileDefaultWorkspace": profile_default_workspace,
        "activeProfile": active_profile,
        "switchWorkspace": switch_workspace,
        "switchResponseWorkspace": switch_response_workspace,
        "showAllProfiles": show_all_profiles,
        "messages": messages or [],
        "timeoutMs": timeout_ms,
        "session": session if session is not None else {"session_id": "session-1"},
    }
    result = subprocess.run(
        [NODE, "-e", _DRIVER, str(SESSIONS_JS), str(PANELS_JS), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"
        )
    out = json.loads(result.stdout.strip().splitlines()[-1])
    return out if return_meta else out["body"]


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


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_project_target_uses_fallback_workspace_before_profile_default():
    body = _run_new_session_case(
        {"project_id": "explicit-project"},
        active_project="active-project",
        all_projects=[
            {
                "project_id": "explicit-project",
                "name": "Explicit",
                "default_workspace": "/workspace/project",
            },
            {
                "project_id": "active-project",
                "name": "Active",
                "default_workspace": "/workspace/active",
            },
        ],
        profile_default_workspace="/workspace/profile",
        session={"session_id": "session-1", "workspace": "/workspace/session"},
    )
    assert body["project_id"] == "explicit-project"
    assert "workspace" not in body
    assert body["fallback_workspace"] == "/workspace/profile"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_active_project_uses_fallback_workspace_before_profile_default():
    body = _run_new_session_case(
        {},
        active_project="active-project",
        all_projects=[
            {
                "project_id": "active-project",
                "name": "Active",
                "default_workspace": "/workspace/active",
            },
        ],
        profile_default_workspace="/workspace/profile",
        session={"session_id": "session-1", "workspace": "/workspace/session"},
    )
    assert body["project_id"] == "active-project"
    assert "workspace" not in body
    assert body["fallback_workspace"] == "/workspace/profile"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_profile_switch_workspace_overrides_project_default_workspace():
    body = _run_new_session_case(
        {},
        active_project="active-project",
        all_projects=[
            {
                "project_id": "active-project",
                "name": "Active",
                "default_workspace": "/workspace/active",
            },
        ],
        profile_default_workspace="/workspace/profile",
        switch_workspace="/workspace/switch",
        session={"session_id": "session-1", "workspace": "/workspace/session"},
    )
    assert body["project_id"] == "active-project"
    assert body["workspace"] == "/workspace/switch"
    assert "fallback_workspace" not in body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_explicit_project_id_none_does_not_use_active_project_default_workspace():
    body = _run_new_session_case(
        {"project_id": None},
        active_project="active-project",
        all_projects=[
            {
                "project_id": "active-project",
                "name": "Active",
                "default_workspace": "/workspace/active",
            },
        ],
        profile_default_workspace="/workspace/profile",
        session={"session_id": "session-1", "workspace": "/workspace/session"},
    )
    assert body["project_id"] is None
    assert body["workspace"] == "/workspace/profile"
    assert "fallback_workspace" not in body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_cross_profile_project_switches_before_request():
    out = _run_new_session_case(
        {"project_id": "foreign-project"},
        all_projects=[
            {
                "project_id": "foreign-project",
                "name": "Foreign",
                "profile": "other",
                "default_workspace": "/workspace/foreign",
            },
        ],
        profile_default_workspace="/workspace/profile",
        switch_workspace="/workspace/profile-switch",
        switch_response_workspace="/workspace/destination-default",
        show_all_profiles=True,
        return_meta=True,
    )
    assert out["timedOut"] is False
    assert out["switchCalls"] == ["other"]
    assert out["activeProfile"] == "other"
    assert out["callCount"] == 1
    assert out["body"]["project_id"] == "foreign-project"
    assert out["body"]["profile"] == "other"
    assert out["body"]["workspace"] == "/workspace/profile-switch"
    assert "fallback_workspace" not in out["body"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_cross_profile_project_new_owns_single_session_creation_during_switch():
    out = _run_new_session_case(
        {"project_id": "foreign-project"},
        all_projects=[
            {
                "project_id": "foreign-project",
                "name": "Foreign",
                "profile": "other",
                "default_workspace": "/workspace/project-default",
            },
        ],
        switch_workspace="/workspace/one-shot",
        switch_response_workspace="/workspace/destination-profile-default",
        session={"session_id": "session-1", "workspace": "/workspace/session"},
        messages=[{"role": "user", "content": "keep"}],
        show_all_profiles=True,
        return_meta=True,
    )
    assert out["timedOut"] is False
    assert out["switchCalls"] == ["other"]
    assert out["callCount"] == 1
    assert out["body"]["project_id"] == "foreign-project"
    assert out["body"]["workspace"] == "/workspace/one-shot"
    assert "fallback_workspace" not in out["body"]


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


# ── #5457: project default workspace selection ────────────────────────────────


def test_resolve_project_helper_exists():
    """`_resolveProjectForNewSession` must be defined in sessions.js."""
    src = _read(SESSIONS_JS)
    assert "function _resolveProjectForNewSession(" in src, (
        "_resolveProjectForNewSession helper not found in sessions.js"
    )


def test_new_session_workspace_precedence_defers_project_default_to_server():
    """Project-targeted newSession() must keep one-shot workspace explicit and defer inherited fallback to the server."""
    src = _read(SESSIONS_JS)
    idx = src.find("async function newSession(")
    assert idx >= 0, "newSession function not found in sessions.js"
    new_session_src = src[idx: idx + 2500]
    # The project resolver must be called before building reqBody
    resolver_idx = new_session_src.find("_resolveProjectForNewSession(")
    req_body_idx = new_session_src.find("const reqBody=")
    assert resolver_idx != -1, "_resolveProjectForNewSession not called in newSession"
    assert resolver_idx < req_body_idx, (
        "_resolveProjectForNewSession must be called before reqBody is built"
    )
    assert "const explicitWs=switchWs||null;" in new_session_src
    assert "if(explicitWs) reqBody.workspace=explicitWs;" in new_session_src
    assert "reqBody.fallback_workspace=fallbackWs;" in new_session_src
    assert "const projectWs=" not in new_session_src


def test_new_session_marks_cross_profile_switch_as_caller_owned():
    src = _read(SESSIONS_JS)
    assert "let _profileSwitchCallerOwnsNewSession = false;" in src
    ensure_idx = src.find("async function _ensureProjectProfileForNewSession(project)")
    assert ensure_idx >= 0, "_ensureProjectProfileForNewSession not found in sessions.js"
    ensure_src = src[ensure_idx: ensure_idx + 600]
    assert "_profileSwitchCallerOwnsNewSession=true;" in ensure_src
    assert "_profileSwitchCallerOwnsNewSession=false;" in ensure_src


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_uses_active_project_default_workspace():
    """When a project is active, newSession() sends fallback_workspace instead of project-derived workspace."""
    all_projects = [
        {"project_id": "proj-ws", "name": "MyProject", "default_workspace": "/home/user/projws"},
    ]
    body = _run_new_session_case({}, active_project="proj-ws", all_projects=all_projects)
    assert body.get("fallback_workspace") is None
    assert "workspace" not in body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_uses_explicit_project_id_default_workspace():
    """When project_id is passed explicitly, the request defers project-default resolution to the server."""
    all_projects = [
        {"project_id": "proj-ws", "name": "MyProject", "default_workspace": "/home/user/projws"},
    ]
    body = _run_new_session_case(
        {"project_id": "proj-ws"},
        active_project=None,
        all_projects=all_projects,
    )
    assert body.get("fallback_workspace") is None
    assert "workspace" not in body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_project_id_none_does_not_inherit_project_workspace():
    """project_id:null means no project — project default workspace must NOT be applied."""
    all_projects = [
        {"project_id": "proj-ws", "name": "MyProject", "default_workspace": "/home/user/projws"},
    ]
    body = _run_new_session_case(
        {"project_id": None},
        active_project="proj-ws",
        all_projects=all_projects,
    )
    assert body.get("workspace") is None
    assert "fallback_workspace" not in body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_project_without_default_workspace_leaves_fallback_workspace_null():
    """A project with no default_workspace still leaves server-side project resolution responsible for workspace."""
    all_projects = [
        {"project_id": "proj-plain", "name": "PlainProject"},
    ]
    body = _run_new_session_case({}, active_project="proj-plain", all_projects=all_projects)
    assert body.get("fallback_workspace") is None
    assert "workspace" not in body
