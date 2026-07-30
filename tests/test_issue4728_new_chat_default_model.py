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

function extractNewSession(src) {
  const start = src.indexOf('async function newSession(');
  if (start < 0) throw new Error('newSession not found');
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
  throw new Error('newSession body not closed');
}

const src = fs.readFileSync(process.argv[2], 'utf8');
const args = JSON.parse(process.argv[3]);
const modelSelect = {
  value: args.currentModel || '',
  options: [],
  selectedOptions: [{
    dataset: {
      provider: args.selectedOptionProvider || '',
    },
  }],
};
const store = new Map();
const captured = [];

function $(id) {
  return id === 'modelSelect' ? modelSelect : null;
}

const localStorage = {
  getItem(key) {
    return store.has(key) ? store.get(key) : null;
  },
  setItem(key, value) {
    store.set(key, String(value));
  },
  removeItem(key) {
    store.delete(key);
  },
};

globalThis.window = globalThis;
globalThis.document = {
  baseURI: 'http://example.test/',
  createElement(tag) {
    return {
      tagName: String(tag || '').toUpperCase(),
      dataset: {},
      appendChild(child) {
        (this.children || (this.children = [])).push(child);
      },
      textContent: '',
      value: '',
    };
  },
};
globalThis.localStorage = localStorage;
globalThis.history = { replaceState() {} };
globalThis.$ = $;
globalThis.NO_PROJECT_FILTER = '__NO_PROJECT_FILTER__';
globalThis._activeProject = '';
globalThis._sessionSourceFilter = 'webui';
globalThis._newSessionInFlight = null;
globalThis._messagesTruncated = false;
globalThis._oldestIdx = 0;
globalThis.INFLIGHT = {};
globalThis.S = {
  session: args.session || null,
  toolCalls: [],
  messages: [],
  activeProfile: args.activeProfile || 'default',
  _pendingSessionToolsets: null,
  _profileSwitchWorkspace: null,
  _profileDefaultWorkspace: null,
};
globalThis._defaultModel = args.defaultModel || null;
globalThis._activeProvider = args.activeProvider || null;
globalThis._emptyComposerModelOverride = args.emptyComposerOverride || null;
globalThis._readPersistedModelState = () => args.persisted || null;
globalThis._readEmptyComposerModelOverride = () => globalThis._emptyComposerModelOverride;
globalThis._clearEmptyComposerModelOverride = () => {
  globalThis._emptyComposerModelOverride = null;
};
globalThis._modelStateForSelect = (sel, modelId) => {
  const value = String(modelId || '').trim();
  if (!value) return { model: '', model_provider: null };
  const provider = String(((sel && sel.selectedOptions && sel.selectedOptions[0] && sel.selectedOptions[0].dataset) || {}).provider || '').trim();
  return {
    model: value,
    model_provider: provider && provider !== 'default' ? provider : null,
  };
};
globalThis._applyModelToDropdown = (modelId, sel, provider) => {
  sel.value = modelId;
  sel._provider = provider || null;
  return true;
};
for (const name of [
  'clearLiveToolCards',
  'updateQueueBadge',
  '_clearPendingSelections',
  'setComposerStatus',
  'setStatus',
  'updateSendBtn',
  'syncTopbar',
  'renderMessages',
  'startSessionStream',
  '_setSessionViewedCount',
  '_setActiveSessionUrl',
  '_rememberNewChatDraftSession',
  '_hydrateTodosFromSession',
  'syncModelChip',
  'syncReasoningChip',
  '_setLiveAssistantTps',
  '_syncCtxIndicator',
  'showToast',
]) {
  globalThis[name] = () => {};
}
globalThis.loadDir = async () => null;
globalThis._setNewSessionPending = () => {};
globalThis.api = async (url, opts) => {
  const body = JSON.parse(opts.body);
  captured.push({ url, body });
  if (url !== '/api/session/new') throw new Error('unexpected api call: ' + url);
  return {
    session: {
      session_id: 'session-1',
      messages: [],
      model: body.model,
      model_provider: body.model_provider,
      workspace: body.workspace,
      message_count: 0,
      last_usage: {},
    },
  };
};

eval(extractNewSession(src));

(async () => {
  await newSession(false, {});
  process.stdout.write(JSON.stringify({
    reqBody: captured[0].body,
    modelValue: modelSelect.value,
    override: globalThis._emptyComposerModelOverride,
  }));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
"""


node_test = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("issue4728_new_chat_default_model") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _run_case(driver_path, payload):
    result = subprocess.run(
        [NODE, driver_path, str(SESSIONS_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)


@node_test
def test_loaded_session_picker_value_posts_configured_default_model(driver_path):
    data = _run_case(driver_path, {
        "session": {
            "session_id": "loaded-session",
            "model": "GPT-5.4",
            "model_provider": "session-provider",
        },
        "currentModel": "GPT-5.4",
        "selectedOptionProvider": "session-provider",
        "defaultModel": "deepseek-v4-flash",
        "activeProvider": "deepseek",
    })

    assert data["reqBody"]["model"] == "deepseek-v4-flash"
    assert data["reqBody"]["model_provider"] == "deepseek"
    assert data["modelValue"] == "deepseek-v4-flash"


@node_test
def test_explicit_empty_composer_override_wins_over_configured_default(driver_path):
    data = _run_case(driver_path, {
        "currentModel": "GPT-5.4",
        "defaultModel": "deepseek-v4-flash",
        "activeProvider": "deepseek",
        "emptyComposerOverride": {
            "model": "cursor/composer-2.5",
            "model_provider": "cursor",
            "saved_at": 123,
        },
    })

    assert data["reqBody"]["model"] == "cursor/composer-2.5"
    assert data["reqBody"]["model_provider"] == "cursor"
    assert data["override"] is None


@node_test
def test_matching_fallback_provider_still_routes_bare_models(driver_path):
    data = _run_case(driver_path, {
        "session": {
            "session_id": "loaded-session",
            "model": "gpt-4o",
            "model_provider": "session-provider",
        },
        "currentModel": "gpt-4o",
        "activeProvider": "openai",
    })

    assert data["reqBody"]["model"] == "gpt-4o"
    assert data["reqBody"]["model_provider"] == "openai"


@node_test
def test_family_mismatched_fallback_provider_stays_null(driver_path):
    data = _run_case(driver_path, {
        "session": {
            "session_id": "loaded-session",
            "model": "gpt-4o",
            "model_provider": "session-provider",
        },
        "currentModel": "gpt-4o",
        "activeProvider": "anthropic",
    })

    assert data["reqBody"]["model"] == "gpt-4o"
    assert data["reqBody"]["model_provider"] is None
