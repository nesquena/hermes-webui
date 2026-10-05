"""Behavioral ownership checks for delete-triggered New Chat settlement."""

from pathlib import Path
import json
import shutil
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = REPO / "static" / "sessions.js"
NODE = shutil.which("node")


_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

function extract(name) {
  const match = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(').exec(src);
  if (!match) throw new Error(name + ' not found');
  const start = match.index;
  const paramsEnd = src.indexOf(')', start);
  let i = src.indexOf('{', paramsEnd) + 1;
  let depth = 1;
  while (depth && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

const store = new Map();
globalThis.localStorage = {
  getItem: key => store.has(key) ? store.get(key) : null,
  setItem: (key, value) => store.set(key, String(value)),
  removeItem: key => store.delete(key),
};
let replacedUrl = null;
globalThis.history = {replaceState(_state, _title, url) { replacedUrl = url; }};
globalThis.window = globalThis;
window.location = {origin: 'http://example.test', pathname: '/session/deleted-A', search: '', hash: ''};
globalThis.document = {baseURI: 'http://example.test/', createElement() { return {dataset:{}, appendChild(){}}; }};
globalThis.$ = () => null;

globalThis.S = {
  session: null,
  messages: [],
  entries: [],
  toolCalls: [],
  activeProfile: 'default',
  _pendingSessionToolsets: null,
  _profileSwitchWorkspace: null,
  _profileDefaultWorkspace: null,
  busy: scenario === 'failure',
  activeStreamId: scenario === 'failure' ? 'stream-of-deleted-A' : null,
};
globalThis._loadSessionGeneration = 0;
globalThis._profileSwitchGeneration = 0;
globalThis._newSessionInFlight = null;
globalThis._messagesTruncated = false;
globalThis._oldestIdx = 0;
globalThis._activeProject = '';
globalThis.NO_PROJECT_FILTER = '__NO_PROJECT_FILTER__';
globalThis._sessionSourceFilter = 'webui';
globalThis._defaultModel = 'test-model';
globalThis._activeProvider = 'test-provider';
globalThis.NEW_CHAT_DRAFT_SESSION_KEY = (src.match(/NEW_CHAT_DRAFT_SESSION_KEY = '([^']+)'/) || [])[1];

let sendButtonUpdates = 0;
for (const name of [
  '_setNewSessionPending', 'updateQueueBadge', 'clearLiveToolCards', 'showToast',
  'assistantDisplayName', 'syncAppTitlebar', 'setComposerStatus', 'setStatus',
]) globalThis[name] = () => {};
globalThis.updateSendBtn = () => { sendButtonUpdates += 1; };
globalThis.syncTopbar = () => {};
globalThis.renderMessages = () => {};
globalThis.loadDir = async () => {};
globalThis._setActiveSessionUrl = () => {};
globalThis.startSessionStream = () => {};
globalThis._setSessionViewedCount = () => {};
window._clearPendingSelections = () => {};
globalThis._appRootPath = () => '/';

let releaseCreate = null;
let releaseDraft = null;
const createWorkspaces = [];
globalThis.api = async (url, options = {}) => {
  if (url.startsWith('/api/session?')) {
    if (scenario === 'draft-load-failure') return {session:{
      session_id:'remembered', message_count:0, title:'New Chat', profile:'default',
      composer_draft:{text:'draft',files:[]}, workspace:'/ws/A',
    }};
    return await new Promise(resolve => {
      releaseDraft = () => resolve({session:{
        session_id:'remembered', message_count:0, title:'New Chat', profile:'default',
        composer_draft:{text:'draft',files:[]},
        workspace:scenario === 'new-chat-during-draft-matching' ? '/ws/A' : '/ws/C',
      }});
    });
  }
  if (url !== '/api/session/new') throw new Error('unexpected API: ' + url);
  const body = JSON.parse(options.body || '{}');
  createWorkspaces.push(body.workspace || null);
  if (scenario === 'failure') throw new Error('create failed');
  if (scenario.startsWith('new-chat-during-draft-')) return {session:{
    session_id:'created-B', messages:[], model:'test-model', model_provider:'test-provider',
    workspace:'/ws/B', message_count:0, last_usage:{},
  }};
  return await new Promise(resolve => {
    releaseCreate = () => resolve({session:{
      session_id:body.workspace === '/ws/B' ? 'created-B' : 'created-A', messages:[], model:'test-model', model_provider:'test-provider',
      workspace:body.workspace || null, message_count:0, last_usage:{},
    }});
  });
};
globalThis.loadSession = async (_sid, options = {}) => {
  const generation = ++_loadSessionGeneration;
  if (typeof options.onLoadClaim === 'function') options.onLoadClaim(generation);
  if (scenario === 'draft-load-failure') throw new Error('remembered draft load failed');
};

eval(extract('_profileMatchesActiveProfile'));
eval(extract('_isRestorableNewChatDraftSession'));
eval(extract('_rememberNewChatDraftSession'));
eval(extract('_clearRememberedNewChatDraftSession'));
eval(extract('_restoreRememberedNewChatDraftSession'));
eval(extract('_deleteNewChatProfileGeneration'));
eval(extract('_deleteNewChatOwnerSnapshot'));
eval(extract('_deleteNewChatOwnerIsCurrent'));
eval(extract('_showEmptyConversationAfterDelete'));
eval(extract('newSession'));
eval(extract('_startNewChatAfterDeletingCurrentSession'));

(async () => {
  if (scenario.startsWith('new-chat-during-draft-')) {
    localStorage.setItem(NEW_CHAT_DRAFT_SESSION_KEY, 'remembered');
  }
  if (scenario === 'draft-load-failure') {
    localStorage.setItem(NEW_CHAT_DRAFT_SESSION_KEY, 'remembered');
  }
  let ordinary = null;
  if (scenario === 'ordinary-b-pending-during-delete-a') {
    S._profileSwitchWorkspace = '/ws/B';
    ordinary = newSession(false);
    while (!releaseCreate) await new Promise(resolve => setTimeout(resolve, 0));
  }
  const owner = _deleteNewChatOwnerSnapshot();
  const pending = _startNewChatAfterDeletingCurrentSession('/ws/A', owner);
  if (scenario === 'superseded' || scenario === 'superseded-profile') {
    while (!releaseCreate) await new Promise(resolve => setTimeout(resolve, 0));
    if (scenario === 'superseded') {
      _loadSessionGeneration += 1;
      S.session = {session_id:'B', workspace:'/ws/B'};
    } else {
      _profileSwitchGeneration += 1;
      S.activeProfile = 'beta';
    }
    releaseCreate();
  } else if (scenario.startsWith('new-chat-during-draft-')) {
    while (!releaseDraft) await new Promise(resolve => setTimeout(resolve, 0));
    S._profileSwitchWorkspace = '/ws/B';
    await newSession(false);
    releaseDraft();
  } else if (scenario === 'ordinary-b-pending-during-delete-a') {
    releaseCreate();
    await ordinary;
  } else if (scenario === 'draft-load-failure') {
    while (!releaseCreate) await new Promise(resolve => setTimeout(resolve, 0));
    releaseCreate();
  }
  const result = await pending;
  process.stdout.write(JSON.stringify({
    activeSid:S.session && S.session.session_id,
    profileWorkspace:S._profileSwitchWorkspace,
    remembered:localStorage.getItem(NEW_CHAT_DRAFT_SESSION_KEY),
    replacedUrl,
    superseded:!!(result && result.superseded),
    failed:!!(result && result.error),
    busy:S.busy,
    activeStreamId:S.activeStreamId,
    sendButtonUpdates,
    createWorkspaces,
  }));
})().catch(error => {
  process.stderr.write(String(error && error.stack || error));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    path = tmp_path_factory.mktemp("delete_new_chat_settlement") / "driver.js"
    path.write_text(_DRIVER, encoding="utf-8")
    return path


def _run(driver, scenario):
    result = subprocess.run(
        [NODE, str(driver), str(SESSIONS_JS), scenario],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_newer_sidebar_navigation_wins_over_pending_new_session_post(driver):
    result = _run(driver, "superseded")
    assert result["activeSid"] == "B"
    assert result["superseded"] is True
    assert result["remembered"] is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_newer_profile_switch_wins_over_pending_new_session_post(driver):
    result = _run(driver, "superseded-profile")
    assert result["activeSid"] is None
    assert result["superseded"] is True
    assert result["remembered"] is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_failed_post_leaves_blank_root_instead_of_deleted_session_route(driver):
    result = _run(driver, "failure")
    assert result["activeSid"] is None
    assert result["profileWorkspace"] is None
    assert result["replacedUrl"] == "/"
    assert result["failed"] is True
    assert result["busy"] is False
    assert result["activeStreamId"] is None
    assert result["sendButtonUpdates"] > 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_failed_remembered_draft_load_falls_back_to_fresh_chat(driver):
    result = _run(driver, "draft-load-failure")
    assert result["activeSid"] == "created-A"
    assert result["createWorkspaces"] == ["/ws/A"]
    assert result["profileWorkspace"] is None
    assert result["superseded"] is False
    assert result["failed"] is False


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_delete_continuation_joins_pending_workspace_without_leaking_override(driver):
    result = _run(driver, "ordinary-b-pending-during-delete-a")
    assert result["activeSid"] == "created-B"
    assert result["createWorkspaces"] == ["/ws/B"]
    assert result["profileWorkspace"] is None
    assert result["superseded"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "scenario",
    ["new-chat-during-draft-matching", "new-chat-during-draft-mismatched"],
)
def test_explicit_new_chat_wins_while_delete_draft_lookup_is_pending(driver, scenario):
    result = _run(driver, scenario)
    assert result["activeSid"] == "created-B"
    assert result["profileWorkspace"] is None
    assert result["superseded"] is True
