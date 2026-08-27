import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
NODE = shutil.which("node")


_DRIVER_SRC = r"""
const fs = require('fs');

function extractFunction(src, signature) {
  const start = src.indexOf(signature);
  if (start < 0) throw new Error(signature + ' not found');
  let depth = 0;
  let bodyStart = src.indexOf('{', src.indexOf(')', start));
  for (let i = bodyStart; i < src.length; i++) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(signature + ' body not closed');
}

const src = fs.readFileSync(process.argv[2], 'utf8');
const args = JSON.parse(process.argv[3]);

function makeElement(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    children: [],
    className: '',
    style: {display: 'none'},
    textContent: '',
    appendChild(child) { this.children.push(child); return child; },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._innerHTML || ''; },
    set(value) {
      this._innerHTML = value;
      if (value === '') this.children = [];
    },
  });
  return el;
}

const elements = {
  modelSelect: {value: args.currentModel, _provider: args.currentProvider},
  topbarTitle: makeElement('div'),
  topbarMeta: makeElement('div'),
  msgInner: makeElement('div'),
  emptyState: makeElement('div'),
  fileTree: makeElement('div'),
  batchActionBar: makeElement('div'),
};
let sessionsRequested = false;
let resolveSessions;
const sessionsResponse = new Promise(resolve => { resolveSessions = resolve; });

globalThis.window = globalThis;
globalThis.$ = id => elements[id] || null;
globalThis.document = {createElement: makeElement};
globalThis.S = {
  session: {session_id: 'active-session'},
  messages: [{role: 'user', content: 'hello'}],
  entries: [{type: 'message'}],
};
globalThis._defaultModel = args.defaultModel;
globalThis._activeProvider = args.defaultProvider;
globalThis._emptyComposerModelOverrideHost = globalThis;
globalThis._emptyComposerModelOverride = null;
globalThis.localStorage = {removeItem() {}};
globalThis._ensureModelOptionInDropdown = (model, select, provider) => {
  select.value = model;
  select._provider = provider || null;
  return model;
};
globalThis.syncReasoningChip = () => {};
globalThis.showConfirmDialog = async () => true;
globalThis._sessionSnapshotById = () => ({session_id: 'active-session'});
globalThis._worktreeSessionCount = () => 0;
globalThis._worktreeResponseCount = () => 0;
globalThis._captureSessionReflowPositions = () => null;
globalThis._clearHandoffStorageForSession = () => {};
globalThis._clearPersistedSessionQueue = () => {};
globalThis._optimisticallyRemoveSessionFromList = () => {};
globalThis._hydrateTodosFromSession = () => {};
globalThis._sessionListQueryString = () => '';
globalThis.assistantDisplayName = () => 'Hermes';
globalThis.syncAppTitlebar = () => {};
globalThis.showToast = () => {};
globalThis._sessionResponseRetainsWorktree = () => false;
globalThis.renderSessionList = async () => {};
globalThis.exitSessionSelectMode = () => {};
globalThis.setStatus = () => {};
globalThis.t = (key, value) => value === undefined ? key : key + ':' + value;
globalThis._optimisticallyRemovedSessionIds = new Set();
globalThis._allSessions = [{session_id: 'active-session'}];
globalThis._pendingSessionReflowPositions = null;
globalThis._selectedSessions = new Set(['active-session']);
globalThis.api = async url => {
  if (url === '/api/session/delete') return {};
  if (url === '/api/sessions') {
    sessionsRequested = true;
    return sessionsResponse;
  }
  throw new Error('unexpected api call: ' + url);
};

eval(extractFunction(src, 'function _rememberEmptyComposerModelOverride('));
eval(extractFunction(src, 'function _readEmptyComposerModelOverride('));
eval(extractFunction(src, 'function _clearEmptyComposerModelOverride('));
eval(extractFunction(src, 'function _resetEmptyComposerModelToConfiguredDefault('));
eval(extractFunction(src, 'async function deleteSession('));
eval(extractFunction(src, 'function _renderBatchActionBar('));

async function waitForSessionsRequest() {
  for (let attempt = 0; attempt < 20 && !sessionsRequested; attempt++) {
    await new Promise(resolve => setImmediate(resolve));
  }
  if (!sessionsRequested) throw new Error('session list request was not reached');
}

(async () => {
  let deletion;
  if (args.mode === 'batch') {
    _renderBatchActionBar();
    const deleteButton = elements.batchActionBar.children.find(child =>
      String(child.className || '').includes('batch-action-btn-danger')
    );
    if (!deleteButton) throw new Error('batch delete button not rendered');
    deletion = deleteButton.onclick();
  } else {
    deletion = deleteSession('active-session');
  }

  await waitForSessionsRequest();
  if (args.pickWhileSettling) {
    elements.modelSelect.value = args.settlingModel;
    elements.modelSelect._provider = args.settlingProvider;
    _rememberEmptyComposerModelOverride(args.settlingModel, args.settlingProvider);
  }
  resolveSessions({sessions: []});
  const deleted = await deletion;
  process.stdout.write(JSON.stringify({
    deleted,
    session: S.session,
    messages: S.messages,
    model: elements.modelSelect.value,
    provider: elements.modelSelect._provider,
    emptyStateDisplay: elements.emptyState.style.display,
    override: _readEmptyComposerModelOverride(),
  }));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("final_session_delete_model_reset") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _run_case(driver_path, **overrides):
    payload = {
        "mode": "single",
        "currentModel": "GPT-5.6 Ornith",
        "currentProvider": "openai-codex",
        "defaultModel": "gpt-5.6-luna",
        "defaultProvider": "openai-codex",
        **overrides,
    }
    result = subprocess.run(
        [NODE, driver_path, str(SESSIONS_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("mode", ["single", "batch"])
def test_deleting_active_final_session_resets_empty_composer_to_configured_default(driver_path, mode):
    data = _run_case(driver_path, mode=mode)

    if mode == "single":
        assert data["deleted"] is True
    assert data["session"] is None
    assert data["messages"] == []
    assert data["model"] == "gpt-5.6-luna"
    assert data["provider"] == "openai-codex"
    assert data["emptyStateDisplay"] == ""
    assert data["override"] is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("mode", ["single", "batch"])
def test_final_session_delete_preserves_model_pick_made_while_session_list_loads(driver_path, mode):
    data = _run_case(
        driver_path,
        mode=mode,
        pickWhileSettling=True,
        settlingModel="claude-sonnet-4-5",
        settlingProvider="anthropic",
    )

    assert data["session"] is None
    assert data["model"] == "claude-sonnet-4-5"
    assert data["provider"] == "anthropic"
    assert data["override"]["model"] == "claude-sonnet-4-5"
    assert data["override"]["model_provider"] == "anthropic"
    assert data["override"]["saved_at"] > 0
