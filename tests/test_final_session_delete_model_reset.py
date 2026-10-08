import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
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

function extractFunctionIfPresent(src, signature) {
  return src.indexOf(signature) >= 0 ? extractFunction(src, signature) : '';
}

function extractStatementBefore(src, startText, endText) {
  const start = src.indexOf(startText);
  if (start < 0) throw new Error(startText + ' not found');
  const end = src.indexOf(endText, start);
  if (end < 0) throw new Error(endText + ' not found after ' + startText);
  return src.slice(start, end);
}

const src = fs.readFileSync(process.argv[2], 'utf8');
const bootSrc = fs.readFileSync(process.argv[3], 'utf8');
const args = JSON.parse(process.argv[4]);

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
let deleteRequested = false;
let resolveDelete;
const deleteResponse = new Promise(resolve => { resolveDelete = resolve; });
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
globalThis._composerModelPickHost = globalThis;
globalThis._composerModelPick = null;
globalThis.localStorage = {removeItem() {}};
globalThis._modelStateForSelect = select => ({
  model: select.value,
  model_provider: select._provider || null,
});
globalThis._ensureModelOptionInDropdown = (model, select, provider) => {
  select.value = model;
  select._provider = provider || null;
  return model;
};
globalThis.syncReasoningChip = () => {};
globalThis.syncModelChip = () => {};
globalThis.syncTopbar = () => {};
globalThis.clearProfileTransitionReasoningContext = () => {};
globalThis.closeModelDropdown = () => {};
globalThis._writePersistedModelState = () => {};
globalThis._rememberPendingSessionModel = () => {};
globalThis._applySessionContextMetadataUpdate = () => {};
globalThis._checkProviderMismatch = () => null;
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
  if (url === '/api/session/delete') {
    deleteRequested = true;
    return deleteResponse;
  }
  if (url === '/api/sessions') {
    sessionsRequested = true;
    return sessionsResponse;
  }
  if (url === '/api/session/update') return {session: {}};
  throw new Error('unexpected api call: ' + url);
};

eval(extractFunction(src, 'function _rememberEmptyComposerModelOverride('));
eval(extractFunction(src, 'function _readEmptyComposerModelOverride('));
eval(extractFunction(src, 'function _clearEmptyComposerModelOverride('));
const rememberComposerModelPickSrc=extractFunctionIfPresent(src, 'function _rememberComposerModelPick(');
if(rememberComposerModelPickSrc) eval(rememberComposerModelPickSrc);
const readComposerModelPickSrc=extractFunctionIfPresent(src, 'function _readComposerModelPick(');
if(readComposerModelPickSrc) eval(readComposerModelPickSrc);
eval(extractFunction(src, 'function _settleEmptyComposerModelAfterFinalSessionDelete('));
eval(extractFunction(src, 'async function deleteSession('));
eval(extractFunction(src, 'function _renderBatchActionBar('));
eval(extractStatementBefore(
  bootSrc,
  "$('modelSelect').onchange=async()=>{",
  "$('msg').addEventListener('input'"
));

async function waitForDeleteRequest() {
  for (let attempt = 0; attempt < 20 && !deleteRequested; attempt++) {
    await new Promise(resolve => setImmediate(resolve));
  }
  if (!deleteRequested) throw new Error('delete request was not reached');
}

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

  await waitForDeleteRequest();
  for (const pick of args.picksWhileDeleting || []) {
    elements.modelSelect.value = pick.model;
    elements.modelSelect._provider = pick.provider;
    await elements.modelSelect.onchange();
  }
  resolveDelete({});

  await waitForSessionsRequest();
  if (args.pickWhileSettling) {
    elements.modelSelect.value = args.settlingModel;
    elements.modelSelect._provider = args.settlingProvider;
    await elements.modelSelect.onchange();
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
        [NODE, driver_path, str(SESSIONS_JS_PATH), str(BOOT_JS_PATH), json.dumps(payload)],
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


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("mode", ["single", "batch"])
def test_final_session_delete_preserves_model_pick_made_while_delete_request_is_pending(driver_path, mode):
    data = _run_case(
        driver_path,
        mode=mode,
        picksWhileDeleting=[
            {"model": "claude-sonnet-4-5", "provider": "anthropic"},
        ],
    )

    assert data["session"] is None
    assert data["model"] == "claude-sonnet-4-5"
    assert data["provider"] == "anthropic"
    assert data["override"]["model"] == "claude-sonnet-4-5"
    assert data["override"]["model_provider"] == "anthropic"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("mode", ["single", "batch"])
def test_final_session_delete_preserves_same_model_pick_from_another_provider(driver_path, mode):
    data = _run_case(
        driver_path,
        mode=mode,
        currentModel="gpt-5.5",
        currentProvider="openai-codex",
        picksWhileDeleting=[
            {"model": "gpt-5.5", "provider": "openrouter"},
        ],
    )

    assert data["model"] == "gpt-5.5"
    assert data["provider"] == "openrouter"
    assert data["override"]["model"] == "gpt-5.5"
    assert data["override"]["model_provider"] == "openrouter"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_final_session_delete_detects_model_pick_even_when_value_returns_to_original(driver_path):
    data = _run_case(
        driver_path,
        picksWhileDeleting=[
            {"model": "claude-sonnet-4-5", "provider": "anthropic"},
            {"model": "GPT-5.6 Ornith", "provider": "openai-codex"},
        ],
    )

    assert data["model"] == "GPT-5.6 Ornith"
    assert data["provider"] == "openai-codex"
    assert data["override"]["model"] == "GPT-5.6 Ornith"
    assert data["override"]["model_provider"] == "openai-codex"
