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
  const start = src.indexOf(`async function ${name}(`);
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
globalThis.syncActiveProjectForWorkspace = (...args) => record('syncActiveProjectForWorkspace', args);
globalThis.cancelEditMode = () => record('cancelEditMode');
globalThis.clearPreview = () => record('clearPreview');
globalThis.showConfirmDialog = async () => true;
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
def test_workspace_picker_creates_new_session_instead_of_mutating_current_session(driver_path):
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
    })

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
