import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
SESSIONS_JS = SESSIONS_JS_PATH.read_text(encoding="utf-8")
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
const elements = {
  modelSelect: {value: args.currentModel, _provider: args.currentProvider},
  topbarTitle: {textContent: ''},
  topbarMeta: {textContent: ''},
  msgInner: {innerHTML: ''},
  emptyState: {style: {display: 'none'}},
  fileTree: {innerHTML: ''},
};
const calls = {clearOverride: 0, reasoningSync: 0};

globalThis.window = globalThis;
globalThis.$ = id => elements[id] || null;
globalThis.S = {
  session: {session_id: 'active-session'},
  messages: [{role: 'user', content: 'hello'}],
  entries: [{type: 'message'}],
};
globalThis._defaultModel = args.defaultModel;
globalThis._activeProvider = args.defaultProvider;
globalThis.localStorage = {removeItem() {}};
globalThis._clearEmptyComposerModelOverride = () => { calls.clearOverride++; };
globalThis._ensureModelOptionInDropdown = (model, select, provider) => {
  select.value = model;
  select._provider = provider || null;
  return model;
};
globalThis.syncReasoningChip = () => { calls.reasoningSync++; };
globalThis.showConfirmDialog = async () => true;
globalThis._sessionSnapshotById = () => ({session_id: 'active-session'});
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
globalThis.setStatus = () => {};
globalThis.t = key => key;
globalThis._optimisticallyRemovedSessionIds = new Set();
globalThis._allSessions = [{session_id: 'active-session'}];
globalThis._pendingSessionReflowPositions = null;
globalThis.api = async url => {
  if (url === '/api/session/delete') return {};
  if (url === '/api/sessions') return {sessions: []};
  throw new Error('unexpected api call: ' + url);
};

eval(extractFunction(src, 'function _resetEmptyComposerModelToConfiguredDefault('));
eval(extractFunction(src, 'async function deleteSession('));

(async () => {
  const deleted = await deleteSession('active-session');
  process.stdout.write(JSON.stringify({
    deleted,
    session: S.session,
    messages: S.messages,
    model: elements.modelSelect.value,
    provider: elements.modelSelect._provider,
    emptyStateDisplay: elements.emptyState.style.display,
    calls,
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


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_deleting_active_final_session_resets_empty_composer_to_configured_default(driver_path):
    result = subprocess.run(
        [
            NODE,
            driver_path,
            str(SESSIONS_JS_PATH),
            json.dumps(
                {
                    "currentModel": "GPT-5.6 Ornith",
                    "currentProvider": "openai-codex",
                    "defaultModel": "gpt-5.6-luna",
                    "defaultProvider": "openai-codex",
                }
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)

    assert data["deleted"] is True
    assert data["session"] is None
    assert data["messages"] == []
    assert data["model"] == "gpt-5.6-luna"
    assert data["provider"] == "openai-codex"
    assert data["emptyStateDisplay"] == ""
    assert data["calls"] == {"clearOverride": 1, "reasoningSync": 1}


def test_single_and_batch_final_session_delete_share_model_reset_helper():
    single_delete = SESSIONS_JS[
        SESSIONS_JS.index("async function deleteSession(") : SESSIONS_JS.index("// ── Project helpers", SESSIONS_JS.index("async function deleteSession("))
    ]
    batch_delete = SESSIONS_JS[
        SESSIONS_JS.index("function _renderBatchActionBar(") : SESSIONS_JS.index("function _showBatchProjectPicker(")
    ]

    assert "_resetEmptyComposerModelToConfiguredDefault();" in single_delete
    assert "_resetEmptyComposerModelToConfiguredDefault();" in batch_delete
