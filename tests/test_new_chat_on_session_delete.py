"""Tests for the opt-in that starts a new chat after deleting the open conversation.

New setting `new_chat_on_session_delete` (default OFF, #5473-style opt-in):
when ON, deleting the conversation you are currently viewing starts a fresh
conversation (reusing your remembered empty New Chat draft when one exists)
instead of loading the most recent remaining conversation.

Two layers of coverage:
  1. static wiring asserts (config/boot/panels/index/i18n) mirroring the
     shipped new_chat_on_workspace_switch opt-in;
  2. a node behaviour test that drives the REAL deleteSession() from
     static/sessions.js against a mocked browser environment and asserts the
     observable effects (newSession vs loadSession, workspace binding).

Mirrors the approach of test_goal_command_js_behaviour.py.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).parent.parent
SESSIONS_JS = REPO / "static" / "sessions.js"

NODE = shutil.which("node")


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


class TestSettingRegistered:
    """new_chat_on_session_delete must default OFF and be a recognized bool key."""

    def test_setting_registered_default_off(self):
        import api.config as c
        assert c._SETTINGS_DEFAULTS.get('new_chat_on_session_delete') is False, (
            "new_chat_on_session_delete must default to False (shipped behavior: "
            "deleting the open conversation loads the most recent remaining one)"
        )
        assert 'new_chat_on_session_delete' in c._SETTINGS_BOOL_KEYS, (
            "new_chat_on_session_delete must be a recognized boolean setting key"
        )


class TestWiring:
    """boot/panels/index/i18n must wire the opt-in flag like the shipped sibling."""

    def test_boot_hydrates_flag(self):
        boot = read('static/boot.js')
        assert 'window._newChatOnSessionDelete=!!s.new_chat_on_session_delete' in boot, (
            "boot.js must set window._newChatOnSessionDelete from the loaded settings"
        )

    def test_panels_wires_checkbox_load_and_payload(self):
        panels = read('static/panels.js')
        assert 'settingsNewChatOnSessionDelete' in panels, (
            "panels.js must wire the settings checkbox (load + apply)"
        )
        assert 'payload.new_chat_on_session_delete' in panels, (
            "panels.js must include new_chat_on_session_delete in the autosave payload"
        )

    def test_settings_checkbox_and_i18n_present(self):
        html = read('static/index.html')
        assert 'id="settingsNewChatOnSessionDelete"' in html, (
            "the Settings checkbox for the opt-in must exist"
        )
        i18n = read('static/i18n.js')
        for key in (
            'settings_label_new_chat_on_session_delete',
            'settings_desc_new_chat_on_session_delete',
        ):
            assert key in i18n, f"i18n key {key} must be defined"


class TestDeleteFlowSource:
    """static/sessions.js structure: flag-gated branch, shared helper, both delete paths."""

    def _delete_body(self):
        src = read('static/sessions.js')
        start = src.find("async function deleteSession(sid, beforeDelete=null)")
        assert start != -1, "deleteSession not found"
        end = src.find("// ── Project helpers", start)
        assert end != -1, "end of deleteSession block not found"
        return src[start:end]

    def test_delete_session_has_gated_new_chat_branch(self):
        body = self._delete_body()
        assert 'window._newChatOnSessionDelete===true' in body, (
            "deleteSession must gate the new-chat branch on the default-off opt-in flag"
        )
        assert 'await _startNewChatAfterDeletingCurrentSession(' in body, (
            "deleteSession must start a new chat via the shared helper when the opt-in is on"
        )
        # The opt-in branch must run BEFORE the shipped fallback that loads the
        # most recent remaining session.
        gate_idx = body.index('window._newChatOnSessionDelete===true')
        fetch_idx = body.index("await api('/api/sessions'+_sessionListQueryString())")
        assert gate_idx < fetch_idx, (
            "the opt-in new-chat branch must short-circuit before the remaining-session fetch"
        )

    def test_shipped_fallback_path_unchanged(self):
        body = self._delete_body()
        # With the setting off the shipped behavior must remain reachable verbatim.
        assert "const remaining=await api('/api/sessions'+_sessionListQueryString());" in body, (
            "the default (opt-in off) path must still fetch the remaining sessions"
        )
        assert 'await loadSession(remaining.sessions[0].session_id);' in body, (
            "the default (opt-in off) path must still load the most recent remaining session"
        )

    def test_both_delete_paths_share_the_helper(self):
        sess_src = read('static/sessions.js')
        # One definition, two call sites (single delete + batch delete).
        assert sess_src.count('async function _startNewChatAfterDeletingCurrentSession(') == 1, (
            "the new-chat helper must be defined exactly once"
        )
        assert sess_src.count('await _startNewChatAfterDeletingCurrentSession(') == 2, (
            "both delete paths (single + batch) must route through the shared helper"
        )
        # The batch block must carry the same gate.
        start = sess_src.find('ids.forEach(_clearHandoffStorageForSession);')
        assert start != -1
        end = sess_src.find('exitSessionSelectMode();await renderSessionList();', start)
        assert end != -1
        batch = sess_src[start:end]
        assert 'window._newChatOnSessionDelete===true' in batch, (
            "the batch delete block must gate on the same opt-in flag"
        )
        assert 'await _startNewChatAfterDeletingCurrentSession(' in batch, (
            "the batch delete block must route through the shared helper"
        )

    def test_helper_restores_draft_else_starts_new_session(self):
        sess_src = read('static/sessions.js')
        start = sess_src.find('async function _startNewChatAfterDeletingCurrentSession(')
        assert start != -1
        end = sess_src.find('\n}', start)
        assert end != -1
        fn = sess_src[start:end]
        assert '_restoreRememberedNewChatDraftSession' in fn, (
            "the helper must reuse the remembered empty New Chat draft when one exists"
        )
        assert 'newSession(false,{' in fn, (
            "the helper must start a fresh session when no draft is remembered"
        )
        assert '_profileSwitchWorkspace' in fn, (
            "the helper must keep the new chat in the deleted conversation's workspace"
        )


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3] || '';
const isBatch = scenario.indexOf('batch_') === 0;

// ---- mocked browser environment ----
const _store = new Map();
global.localStorage = {
  getItem: k => (_store.has(k) ? _store.get(k) : null),
  setItem: (k, v) => { _store.set(k, String(v)); },
  removeItem: k => { _store.delete(k); },
};
global.sessionStorage = {
  getItem: k => (_store.has('s:' + k) ? _store.get('s:' + k) : null),
  setItem: (k, v) => { _store.set('s:' + k, String(v)); },
  removeItem: k => { _store.delete('s:' + k); },
};
global.window = {};
global.history = { replaceState() {} };

// minimal DOM + batch-action-bar doubles for the batch-delete route
const _barChildren = [];
const _bar = { style: {}, innerHTML: '', appendChild: c => { _barChildren.push(c); } };
function _el() {
  return { className: '', textContent: '', style: {}, dataset: {}, appendChild() {}, closest() { return null; }, remove() {}, setAttribute() {}, addEventListener() {}, onclick: null };
}
global.document = { querySelectorAll: () => [], createElement: () => _el(), addEventListener() {}, removeEventListener() {} };

// ---- observable effects the scenarios assert ----
const out = {
  restoreCalls: 0,
  newSessionCalls: 0,
  loadSessionArgs: [],
  sessionsFetchCalls: 0,
  deleteCalls: 0,
  workspaceFlagAtNewSession: null,
  toasts: [],
  renderListCalls: 0,
  rememberedKeyAfter: null,
  newSessionOptions: null,
};

// ---- stubs the extracted deleteSession references ----
var _pendingSessionReflowPositions = null;
var _loadSessionGeneration = 0;
var _profileSwitchGeneration = 0;
var _allSessions = [];
const _optimisticallyRemovedSessionIds = new Set();
const t = k => k;
const showToast = m => { out.toasts.push(String(m)); };
const setStatus = () => {};
const assistantDisplayName = () => 'Hermes';
const syncAppTitlebar = () => {};
const _appRootPath = () => '/';
const $ = id => (id === 'batchActionBar' ? _bar : null);
const renderSessionListFromCache = () => {};
const renderSessionList = async () => { out.renderListCalls += 1; };
const _sessionListQueryString = () => '';
const _sessionSnapshotById = sid => ({ session_id: sid, worktree_path: null });
const _captureSessionReflowPositions = () => null;
const _clearHandoffStorageForSession = () => {};
const _clearPersistedSessionQueue = () => {};
const _hydrateTodosFromSession = () => {};
const _optimisticallyRemoveSessionFromList = () => {};
const _sessionResponseRetainsWorktree = () => false;
const showConfirmDialog = async () => true;
const _selectedSessions = new Set();
const _worktreeSessionCount = () => 0;
const _worktreeResponseCount = () => 0;
const exitSessionSelectMode = () => {};

let releaseDraft = null;
async function api(url) {
  if (url === '/api/session/delete') { out.deleteCalls += 1; return {}; }
  if (url.indexOf('/api/session?') === 0) {
    if (scenario === 'flag_on_delayed_draft_navigate_b' || scenario === 'flag_on_delayed_draft_switch_profile') {
      return await new Promise(resolve => { releaseDraft = () => resolve({session: global.__draftSession}); });
    }
    return { session: global.__draftSession };
  }
  if (url.indexOf('/api/sessions') === 0) {
    out.sessionsFetchCalls += 1;
    return { sessions: [{ session_id: 'remaining-1' }] };
  }
  return {};
}
async function loadSession(sid) { out.loadSessionArgs.push(sid); S.session = { session_id: sid }; }
const NEW_CHAT_DRAFT_SESSION_KEY = (src.match(/NEW_CHAT_DRAFT_SESSION_KEY = '([^']+)'/) || [])[1];
if (!NEW_CHAT_DRAFT_SESSION_KEY) throw new Error('NEW_CHAT_DRAFT_SESSION_KEY not found in sessions.js');
const DRAFT_SCEN = {
  flag_on_draft_other_ws: '/ws/B',
  flag_on_draft_same_ws: '/ws/A',
  batch_flag_on_draft_other_ws: '/ws/B',
  batch_flag_on_draft_same_ws: '/ws/A',
  flag_on_delayed_draft_navigate_b: '/ws/A',
  flag_on_delayed_draft_switch_profile: '/ws/A',
};
var _draftRestorable = { value: scenario === 'flag_on_draft_restored' };
var _restoreRememberedNewChatDraftSession;
if (DRAFT_SCEN[scenario]) {
  _store.set(NEW_CHAT_DRAFT_SESSION_KEY, 'remembered-1');
  global.__draftSession = { session_id: 'remembered-1', message_count: 0, title: 'New Chat', profile: 'default', composer_draft: { text: 'remembered draft', files: [] }, workspace: DRAFT_SCEN[scenario] };
} else {
  _restoreRememberedNewChatDraftSession = async function () {
    out.restoreCalls += 1;
    return _draftRestorable.value;
  };
}
async function newSession(flash, options = {}) {
  out.newSessionCalls += 1;
  out.workspaceFlagAtNewSession = S._profileSwitchWorkspace || null;
  out.newSessionOptions = {
    preserveRememberedDraftPointer: !!options.preserveRememberedDraftPointer,
    hasOwnerGuard: typeof options.stillOwnsPane === 'function',
  };
  if (scenario === 'flag_on_new_session_failure') throw new Error('create failed');
  if (!options.preserveRememberedDraftPointer) {
    _store.set(NEW_CHAT_DRAFT_SESSION_KEY, 'created-in-A');
  }
}

// ---- state ----
const S = {
  session: { session_id: 'A', workspace: '/ws/A' },
  messages: [], entries: [], activeProfile: 'default',
  _profileSwitchWorkspace: null, _profileDefaultWorkspace: null,
};

// ---- scenario fixtures ----
const FLAGS = {
  flag_on_current_deleted: true,
  flag_on_draft_restored: true,
  flag_on_other_deleted: true,
  flag_off_current_deleted: false,
  flag_on_draft_other_ws: true,
  flag_on_draft_same_ws: true,
  batch_flag_on_draft_other_ws: true,
  batch_flag_on_draft_same_ws: true,
  flag_on_delayed_draft_navigate_b: true,
  flag_on_delayed_draft_switch_profile: true,
  flag_on_new_session_failure: true,
};
window._newChatOnSessionDelete = FLAGS[scenario];
const deleteTarget = scenario === 'flag_on_other_deleted' ? 'B' : 'A';

// ---- extract functions from the real sessions.js and evaluate them ----
function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const m = re.exec(src);
  if (!m) throw new Error(name + ' not found');
  const start = m.index;
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
// The shared new-chat helper only exists after the feature lands; DELETE against
// the pre-feature source must fail on the scenario ASSERTIONS, not extraction.
try {
  eval(extractFunc('_deleteNewChatProfileGeneration'));
  eval(extractFunc('_deleteNewChatOwnerSnapshot'));
  eval(extractFunc('_deleteNewChatOwnerIsCurrent'));
  eval(extractFunc('_showEmptyConversationAfterDelete'));
  eval(extractFunc('_startNewChatAfterDeletingCurrentSession'));
} catch (e) {}
eval(extractFunc('deleteSession'));
if (DRAFT_SCEN[scenario]) {
  eval(extractFunc('_profileMatchesActiveProfile'));
  eval(extractFunc('_isRestorableNewChatDraftSession'));
  eval(extractFunc('_clearRememberedNewChatDraftSession'));
  eval(extractFunc('_restoreRememberedNewChatDraftSession'));
}
if (isBatch) eval(extractFunc('_renderBatchActionBar'));

(async () => {
  if (scenario === 'flag_on_delayed_draft_navigate_b' || scenario === 'flag_on_delayed_draft_switch_profile') {
    const pending = deleteSession(deleteTarget);
    while (!releaseDraft) await new Promise(resolve => setTimeout(resolve, 0));
    if (scenario === 'flag_on_delayed_draft_navigate_b') {
      _loadSessionGeneration += 1;
      S.session = {session_id: 'B', workspace: '/ws/B'};
    } else {
      _profileSwitchGeneration += 1;
      S.activeProfile = 'beta';
    }
    releaseDraft();
    await pending;
  } else if (isBatch) {
    _selectedSessions.add('A');
    _renderBatchActionBar();
    const deleteBtn = _barChildren.filter(c => c.className && c.className.indexOf('batch-action-btn-danger') !== -1)[0];
    if (!deleteBtn) throw new Error('batch delete button not found');
    await deleteBtn.onclick();
  } else {
    await deleteSession(deleteTarget);
  }
  out.rememberedKeyAfter = _store.has(NEW_CHAT_DRAFT_SESSION_KEY) ? _store.get(NEW_CHAT_DRAFT_SESSION_KEY) : null;
  process.stdout.write(JSON.stringify(out));
})().catch(e => {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    """Write the node driver to a tmp file (works around `node -e` arg quirks)."""
    p = tmp_path_factory.mktemp("newchat_delete_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _run_scenario(driver_path, scenario):
    """Run deleteSession against the real static/sessions.js with mocked browser state."""
    result = subprocess.run(
        [NODE, driver_path, str(SESSIONS_JS), scenario],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed for {scenario}: {result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
class TestDeleteFlowBehaviour:
    """Drive the REAL deleteSession() and assert observable delete→new-chat effects."""

    def test_delete_open_session_starts_new_chat_when_optin_on(self, driver_path):
        out = _run_scenario(driver_path, "flag_on_current_deleted")
        assert out["deleteCalls"] == 1
        assert out["newSessionCalls"] == 1, (
            "with the opt-in on, deleting the open conversation must start a new chat"
        )
        assert out["loadSessionArgs"] == [], (
            "with the opt-in on, no remaining session may be loaded"
        )
        assert out["sessionsFetchCalls"] == 0, (
            "with the opt-in on, the remaining-session list must not be fetched"
        )
        assert out["restoreCalls"] == 1, (
            "the remembered New Chat draft must be consulted first"
        )
        assert out["workspaceFlagAtNewSession"] == "/ws/A", (
            "the new chat must keep the deleted conversation's workspace"
        )
        assert any("session_deleted" in m for m in out["toasts"]), (
            "the delete confirmation toast must still fire"
        )

    def test_delete_open_session_reuses_remembered_draft_when_present(self, driver_path):
        out = _run_scenario(driver_path, "flag_on_draft_restored")
        assert out["restoreCalls"] == 1
        assert out["newSessionCalls"] == 0, (
            "when a restorable empty draft exists, the helper must reuse it instead "
            "of minting another empty session"
        )
        assert out["workspaceFlagAtNewSession"] is None, (
            "the workspace one-shot flag must not leak when the draft path wins"
        )

    def test_delete_open_session_loads_most_recent_when_optin_off(self, driver_path):
        out = _run_scenario(driver_path, "flag_off_current_deleted")
        assert out["loadSessionArgs"] == ["remaining-1"], (
            "with the opt-in off (default), the shipped fallback must still load "
            "the most recent remaining session"
        )
        assert out["newSessionCalls"] == 0
        assert out["restoreCalls"] == 0

    def test_deleting_other_session_unaffected_when_optin_on(self, driver_path):
        out = _run_scenario(driver_path, "flag_on_other_deleted")
        assert out["deleteCalls"] == 1
        assert out["newSessionCalls"] == 0, (
            "deleting a NON-open conversation must not start a new chat"
        )
        assert out["loadSessionArgs"] == []
        assert out["sessionsFetchCalls"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
class TestDraftWorkspaceAwareness:
    """Review finding (2026-09-29): the remembered empty-draft reuse in the
    delete flow must be workspace-aware. A draft remembered for a different
    workspace must not be loaded (and must stay remembered for the ordinary
    New Chat flow); a draft for the deleted workspace must still be reused.
    Both the single-delete and the batch-delete routes carry the invariant.
    """

    def test_delete_open_session_skips_draft_from_other_workspace(self, driver_path):
        out = _run_scenario(driver_path, "flag_on_draft_other_ws")
        assert out["loadSessionArgs"] == [], (
            "the delete flow must not load a draft remembered for another workspace"
        )
        assert out["newSessionCalls"] == 1, (
            "the delete flow must fall through to a fresh chat in the deleted workspace"
        )
        assert out["workspaceFlagAtNewSession"] == "/ws/A", (
            "the fresh chat must stay in the deleted conversation's workspace"
        )
        assert out["rememberedKeyAfter"] == "remembered-1", (
            "the workspace-mismatched draft must stay remembered for the ordinary New Chat flow"
        )
        assert out["newSessionOptions"] == {
            "preserveRememberedDraftPointer": True,
            "hasOwnerGuard": True,
        }

    def test_delete_open_session_restores_draft_from_same_workspace(self, driver_path):
        out = _run_scenario(driver_path, "flag_on_draft_same_ws")
        assert out["loadSessionArgs"] == ["remembered-1"], (
            "a draft belonging to the deleted conversation's workspace must still be reused"
        )
        assert out["newSessionCalls"] == 0
        assert out["rememberedKeyAfter"] == "remembered-1"

    def test_batch_delete_skips_draft_from_other_workspace(self, driver_path):
        out = _run_scenario(driver_path, "batch_flag_on_draft_other_ws")
        assert out["deleteCalls"] == 1
        assert out["loadSessionArgs"] == []
        assert out["newSessionCalls"] == 1
        assert out["workspaceFlagAtNewSession"] == "/ws/A"
        assert out["rememberedKeyAfter"] == "remembered-1"

    def test_batch_delete_restores_draft_from_same_workspace(self, driver_path):
        out = _run_scenario(driver_path, "batch_flag_on_draft_same_ws")
        assert out["loadSessionArgs"] == ["remembered-1"]
        assert out["newSessionCalls"] == 0

    def test_pending_draft_lookup_cannot_steal_newer_navigation(self, driver_path):
        out = _run_scenario(driver_path, "flag_on_delayed_draft_navigate_b")
        assert out["loadSessionArgs"] == []
        assert out["newSessionCalls"] == 0
        assert out["rememberedKeyAfter"] == "remembered-1"

    def test_pending_draft_lookup_cannot_steal_newer_profile_switch(self, driver_path):
        out = _run_scenario(driver_path, "flag_on_delayed_draft_switch_profile")
        assert out["loadSessionArgs"] == []
        assert out["newSessionCalls"] == 0
        assert out["rememberedKeyAfter"] == "remembered-1"

    def test_new_session_failure_does_not_report_delete_as_failed(self, driver_path):
        out = _run_scenario(driver_path, "flag_on_new_session_failure")
        assert out["deleteCalls"] == 1
        assert out["newSessionCalls"] == 1
        assert any("session_deleted" in item for item in out["toasts"])
        assert any("starting a new chat failed" in item for item in out["toasts"])
        assert not any(item == "Delete failed: create failed" for item in out["toasts"])
