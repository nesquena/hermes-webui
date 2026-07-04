import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
NODE = shutil.which("node")


_DRIVER_SRC = r"""
const fs = require('fs');

function extractFunction(src, name) {
  let start = src.indexOf(`async function ${name}(`);
  if (start < 0) start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found`);
  const bodyStart = src.indexOf('{', src.indexOf(')', start));
  let depth = 0;
  for (let i = bodyStart; i < src.length; i++) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`${name} body not closed`);
}

const src = fs.readFileSync(process.argv[2], 'utf8');
const args = JSON.parse(process.argv[3]);
const calls = [];

function record(name, payload) {
  calls.push({ name, payload: payload || null });
}

function t(key, ...values) {
  if (key === 'workspace_switched_to') return `Switched to ${values[0] || ''}`;
  if (key === 'workspace_switch_new_chat_confirm_title') return 'Start a new chat for this workspace?';
  if (key === 'workspace_switch_new_chat_confirm_message') return `Switching to ${values[0] || ''} in the current conversation keeps the existing chat history.`;
  if (key === 'workspace_switch_new_chat_confirm') return 'Start new chat';
  if (key === 'workspace_switch_keep_current') return 'Keep current';
  return key + (values.length ? ':' + values.join(',') : '');
}

const S = {
  session: args.session || null,
  messages: args.messages || [{ role: 'user', content: 'existing turn' }],
  busy: !!args.busy,
  activeProfile: args.activeProfile || 'default',
  _profileSwitchWorkspace: null,
  _pendingSessionToolsets: 'keep-before-switch',
};

globalThis.S = S;
globalThis.t = t;
globalThis._previewDirty = false;
globalThis._currentPanel = '';
globalThis.closeWsDropdown = () => record('closeWsDropdown');
globalThis.getWorkspaceFriendlyName = (path) => String(path).split('/').filter(Boolean).pop() || path;
globalThis.showToast = (...args) => record('showToast', args);
globalThis.setStatus = (...args) => record('setStatus', args);
globalThis.loadDir = async (...args) => record('loadDir', args);
globalThis.loadMemory = async (...args) => record('loadMemory', args);
globalThis.syncTopbar = () => record('syncTopbar');
globalThis.renderMessages = () => record('renderMessages');
globalThis.renderSessionList = async () => record('renderSessionList');
globalThis._setActiveProjectFilter = (...args) => record('_setActiveProjectFilter', args);
globalThis.cancelEditMode = () => record('cancelEditMode');
globalThis.clearPreview = () => record('clearPreview');
globalThis.showConfirmDialog = async (opts) => {
  record('showConfirmDialog', opts);
  return !!args.confirmStartNewChat;
};
globalThis.api = async (url, opts = {}) => {
  const body = opts.body ? JSON.parse(opts.body) : null;
  record('api', { url, body });
  if (url === '/api/session/update') {
    return { session: { ...S.session, workspace: body.workspace } };
  }
  if (url === '/api/session/new') {
    return {
      session: {
        session_id: 'new-session',
        workspace: body.workspace,
        profile: body.profile || null,
        project_id: body.project_id || null,
        messages: [],
        model: body.model || '',
        model_provider: body.model_provider || null,
      },
    };
  }
  throw new Error('unexpected api call: ' + url);
};
globalThis.newSession = async (flash, options = {}) => {
  record('newSession', { flash: !!flash, options, workspaceHint: S._profileSwitchWorkspace });
  const oldSid = S.session && S.session.session_id;
  const body = {
    workspace: S._profileSwitchWorkspace || null,
    profile: S.activeProfile || 'default',
  };
  if (oldSid) body.prev_session_id = oldSid;
  if (Object.prototype.hasOwnProperty.call(options, 'project_id')) body.project_id = options.project_id;
  const data = await globalThis.api('/api/session/new', { method: 'POST', body: JSON.stringify(body) });
  S.session = data.session;
  S.messages = data.session.messages || [];
  return data;
};

eval(extractFunction(src, '_workspaceSwitchHasConversation'));
eval(extractFunction(src, '_syncWorkspaceSwitchProject'));
eval(extractFunction(src, '_switchWorkspaceInCurrentSession'));
eval(extractFunction(src, '_switchWorkspaceWithNewSession'));
eval(extractFunction(src, 'switchToWorkspace'));

(async () => {
  await switchToWorkspace(args.targetWorkspace, args.targetName || 'Target');
  process.stdout.write(JSON.stringify({ calls, session: S.session, messages: S.messages, switchWorkspace: S._profileSwitchWorkspace }));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
"""


node_test = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("workspace_switch_new_session") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _run_case(driver_path, payload):
    assert NODE is not None
    result = subprocess.run(
        [NODE, driver_path, str(PANELS_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)


@node_test
def test_workspace_picker_prompts_then_starts_new_session_when_confirmed(driver_path):
    data = _run_case(driver_path, {
        "session": {
            "session_id": "old-session",
            "workspace": "/repo-a",
            "model": "openai/gpt-5.4-mini",
            "model_provider": "openai",
        },
        "activeProfile": "dev-profile",
        "targetWorkspace": "/repo-b",
        "targetName": "Repo B",
        "confirmStartNewChat": True,
    })

    confirm_calls = [c["payload"] for c in data["calls"] if c["name"] == "showConfirmDialog"]
    assert len(confirm_calls) == 1
    assert confirm_calls[0]["title"] == "Start a new chat for this workspace?"
    assert confirm_calls[0]["confirmLabel"] == "Start new chat"
    assert confirm_calls[0]["cancelLabel"] == "Keep current"
    assert confirm_calls[0]["focusCancel"] is True
    api_calls = [c["payload"] for c in data["calls"] if c["name"] == "api"]
    assert [call["url"] for call in api_calls] == ["/api/session/new"]
    assert api_calls[0]["body"]["workspace"] == "/repo-b"
    assert api_calls[0]["body"]["prev_session_id"] == "old-session"
    assert api_calls[0]["body"]["profile"] == "dev-profile"
    assert data["session"]["session_id"] == "new-session"
    assert data["session"]["workspace"] == "/repo-b"
    assert data["messages"] == []


@node_test
def test_selecting_current_workspace_does_not_create_a_new_session(driver_path):
    data = _run_case(driver_path, {
        "session": {
            "session_id": "old-session",
            "workspace": "/repo-a",
            "model": "openai/gpt-5.4-mini",
            "model_provider": "openai",
        },
        "targetWorkspace": "/repo-a",
        "targetName": "Repo A",
    })

    assert not [c for c in data["calls"] if c["name"] in {"api", "newSession"}]
    assert data["session"]["session_id"] == "old-session"
    assert data["session"]["workspace"] == "/repo-a"


@node_test
def test_workspace_picker_keep_current_updates_existing_session(driver_path):
    data = _run_case(driver_path, {
        "session": {
            "session_id": "old-session",
            "workspace": "/repo-a",
            "model": "openai/gpt-5.4-mini",
            "model_provider": "openai",
            "project_id": "project-a",
        },
        "targetWorkspace": "/repo-b",
        "targetName": "Repo B",
        "confirmStartNewChat": False,
    })

    assert [c["name"] for c in data["calls"] if c["name"] == "showConfirmDialog"] == ["showConfirmDialog"]
    api_calls = [c["payload"] for c in data["calls"] if c["name"] == "api"]
    assert [call["url"] for call in api_calls] == ["/api/session/update"]
    assert api_calls[0]["body"] == {
        "session_id": "old-session",
        "workspace": "/repo-b",
        "model": "openai/gpt-5.4-mini",
        "model_provider": "openai",
    }
    assert not [c for c in data["calls"] if c["name"] == "newSession"]
    assert data["session"]["session_id"] == "old-session"
    assert data["session"]["workspace"] == "/repo-b"
    assert data["session"]["project_id"] is None


@node_test
def test_blank_workspace_switch_creates_new_session_without_prompt(driver_path):
    data = _run_case(driver_path, {
        "session": None,
        "messages": [],
        "activeProfile": "dev-profile",
        "targetWorkspace": "/repo-b",
        "targetName": "Repo B",
    })

    assert not [c for c in data["calls"] if c["name"] == "showConfirmDialog"]
    api_calls = [c["payload"] for c in data["calls"] if c["name"] == "api"]
    assert [call["url"] for call in api_calls] == ["/api/session/new"]
    assert api_calls[0]["body"]["workspace"] == "/repo-b"
    assert "prev_session_id" not in api_calls[0]["body"]
    assert data["session"]["session_id"] == "new-session"
    assert data["session"]["workspace"] == "/repo-b"
