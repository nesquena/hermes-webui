import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static/ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static/panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static/i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_js_block(src: str, marker: str, start_at: int = 0) -> str:
    start = src.index(marker, start_at)
    brace = src.index("{", start)
    depth = 1
    i = brace
    while depth and i + 1 < len(src):
        i += 1
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
    return src[start:i + 1]


def _extract_js_function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    params_start = src.index("(", start)
    depth = 1
    i = params_start
    while depth and i + 1 < len(src):
        i += 1
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
    brace = src.index("{", i)
    depth = 1
    j = brace
    while depth and j + 1 < len(src):
        j += 1
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
    return src[start:j + 1]


def _render_one_session_prefix(src: str) -> str:
    start = src.index("function _renderOneSession(s, isPinnedGroup=false){")
    end = src.index("    const swipeReturnOffset=", start)
    return src[start:end] + "    return el;\n}"


def _run_node(source: str) -> dict:
    script_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".js",
            dir=ROOT,
            delete=False,
        ) as handle:
            handle.write(source)
            script_path = handle.name
        result = subprocess.run(
            [NODE, script_path],
            cwd=ROOT,
            capture_output=True,
            encoding="utf-8",
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout)
    finally:
        if script_path:
            os.unlink(script_path)


def test_activity_ui_uses_sidebar_scope_query_and_stable_tray_nodes():
    assert "/api/activity/active-runs" in UI_JS
    refresh_start = UI_JS.index("async function refreshActiveRunVisibility()")
    assert "/health" not in UI_JS[refresh_start:refresh_start + 1500]
    assert "_openSidebarSession(knownSession)" in UI_JS
    assert "refreshSessionList('active-run-visibility')" in UI_JS
    assert "qs.set('all_profiles', '1')" in UI_JS
    assert "qs.set('exclude_hidden', '1')" in UI_JS
    assert "t('active_run_conversation_fallback')" in UI_JS
    assert "t('active_run_open_conversation')" in UI_JS
    assert "t('active_run_visibility_label'" in UI_JS
    assert "activeRunPill" in INDEX_HTML and "activeRunTray" in INDEX_HTML
    pill_markup = INDEX_HTML[INDEX_HTML.index('id="activeRunPill"'):INDEX_HTML.index('id="activeRunPill"') + 220]
    assert 'aria-haspopup="true"' not in pill_markup
    assert 'aria-expanded="false"' in INDEX_HTML
    assert 'aria-controls="activeRunTray"' in INDEX_HTML
    assert 'role="list"' in INDEX_HTML
    assert ".active-run-visibility" in STYLE_CSS and "-webkit-app-region:no-drag;" in STYLE_CSS
    assert "active_run_visibility_label" in I18N_JS
    assert "Array.from(_activeRunSessionIds).sort()" in SESSIONS_JS

    source = f"""
{_extract_js_function(UI_JS, '_activeRunDuration')}
{_extract_js_function(UI_JS, '_activeRunSessionLabel')}
{_extract_js_function(UI_JS, '_activeRunKnownSession')}
{_extract_js_function(UI_JS, '_activeRunScopeQuery')}
{_extract_js_function(UI_JS, '_activeRunSessionIdsChanged')}
{_extract_js_function(UI_JS, '_syncActiveRunSessionIds')}
{_extract_js_function(UI_JS, '_hideActiveRunTray')}
{_extract_js_function(UI_JS, '_syncActiveRunTray')}
{_extract_js_function(UI_JS, '_renderActiveRunVisibility')}

function makeNode(tag) {{
  return {{
    tagName: String(tag || '').toUpperCase(),
    className: '',
    dataset: {{}},
    style: {{}},
    children: [],
    hidden: false,
    textContent: '',
    title: '',
    type: '',
    parentNode: null,
    attributes: {{}},
    classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
    appendChild(child) {{
      if (child.parentNode) {{
        child.parentNode.children = child.parentNode.children.filter(node => node !== child);
      }}
      child.parentNode = this;
      this.children.push(child);
      return child;
    }},
    append(...children) {{
      children.forEach(child => this.appendChild(child));
    }},
    remove() {{
      if (this.parentNode) {{
        this.parentNode.children = this.parentNode.children.filter(node => node !== this);
        this.parentNode = null;
      }}
    }},
    addEventListener() {{}},
    setAttribute(name, value) {{
      this.attributes[name] = String(value);
    }},
    getAttribute(name) {{
      return Object.prototype.hasOwnProperty.call(this.attributes, name)
        ? this.attributes[name]
        : null;
    }},
    contains(target) {{
      if (this === target) return true;
      return this.children.some(child => typeof child.contains === 'function' && child.contains(target));
    }},
    focus() {{
      this.focused = true;
    }},
  }};
}}

const elements = {{
  activeRunVisibility: makeNode('div'),
  activeRunPill: makeNode('button'),
  activeRunTray: makeNode('div'),
}};
function $(id) {{
  return elements[id] || null;
}}

let refreshCalls = 0;
let openSidebarCalls = 0;
let loadSessionCalls = 0;
global.document = {{createElement: makeNode}};
global.URLSearchParams = URLSearchParams;
global.S = {{session: null, busy: false}};
global._allSessions = [
  {{session_id: 'active-a', title: 'Alpha'}},
  {{session_id: 'active-b', title: 'Beta'}},
];
global._activeProject = 'project-1';
global._showAllProfiles = true;
global._activeRunSnapshot = {{runs: []}};
global._activeRunSessionIds = new Set();
global._requestedSessionSidebarSource = () => 'cli';
global._sessionListExcludeHiddenEnabled = () => true;
global.refreshSessionList = async () => {{ refreshCalls += 1; }};
global._openSidebarSession = async (session) => {{ openSidebarCalls += 1; global._openedSessionId = session.session_id; }};
global.loadSession = async () => {{ loadSessionCalls += 1; }};
global.t = (key, ...args) => {{
  if (key === 'active_run_conversation_fallback') return 'Active conversation';
  if (key === 'active_run_open_conversation') return 'Open conversation';
  if (key === 'active_run_visibility_label') return `${{args[0]}} active · ${{args[1]}}`;
  return key;
}};

async function main() {{
  const query = _activeRunScopeQuery();

  _activeRunSnapshot = {{
    runs: [{{session_id: 'active-a', age_seconds: 12}}],
    oldest_run_age_seconds: 12,
  }};
  _renderActiveRunVisibility();
  const firstButton = elements.activeRunTray.children[0]._button;
  await firstButton.onclick();
  const refreshAfterFirst = refreshCalls;

  _activeRunSnapshot = {{
    runs: [{{session_id: 'active-a', age_seconds: 45}}],
    oldest_run_age_seconds: 45,
  }};
  _renderActiveRunVisibility();
  const refreshAfterSecond = refreshCalls;
  const sameButton = firstButton === elements.activeRunTray.children[0]._button;
  const updatedAge = elements.activeRunTray.children[0]._age.textContent;

  _activeRunSnapshot = {{
    runs: [{{session_id: 'active-b', age_seconds: 8}}],
    oldest_run_age_seconds: 8,
  }};
  _renderActiveRunVisibility();
  const refreshAfterThird = refreshCalls;
  const switchedButton = firstButton !== elements.activeRunTray.children[0]._button;

  _activeRunSnapshot = {{runs: []}};
  _renderActiveRunVisibility();
  const refreshAfterFourth = refreshCalls;
  elements.activeRunTray.hidden = false;
  _hideActiveRunTray({{restoreFocus:true}});

  console.log(JSON.stringify({{
    query,
    refreshAfterFirst,
    refreshAfterSecond,
    refreshAfterThird,
    refreshAfterFourth,
    sameButton,
    switchedButton,
    updatedAge,
    openSidebarCalls,
    loadSessionCalls,
    openedSessionId: global._openedSessionId,
    hostHiddenAfterEmpty: elements.activeRunVisibility.hidden,
    trayHiddenAfterEmpty: elements.activeRunTray.hidden,
    focusRestored: elements.activeRunPill.focused === true,
  }}));
}}

main().catch(err => {{
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
}});
"""
    assert _run_node(source) == {
        "query": "?sidebar_source=cli&project_id=project-1&all_profiles=1&exclude_hidden=1",
        "refreshAfterFirst": 1,
        "refreshAfterSecond": 1,
        "refreshAfterThird": 2,
        "refreshAfterFourth": 3,
        "sameButton": True,
        "switchedButton": True,
        "updatedAge": "45s",
        "openSidebarCalls": 1,
        "loadSessionCalls": 0,
        "openedSessionId": "active-a",
        "hostHiddenAfterEmpty": True,
        "trayHiddenAfterEmpty": True,
        "focusRestored": True,
    }


def test_active_run_locale_keys_are_present_in_every_locale_block():
    keys = {
        "active_run_conversation_fallback",
        "active_run_open_conversation",
        "active_run_visibility_label",
    }
    blocks = re.findall(r"^  ([\w-]+): \{(.*?)(?=^  [\w-]+: \{|\Z)", I18N_JS, re.MULTILINE | re.DOTALL)
    assert blocks
    for locale, block in blocks:
        assert keys <= set(re.findall(r"^\s+(active_run_[a-z_]+):", block, re.MULTILINE)), locale


def test_active_run_delayed_response_cannot_repaint_a_new_scope():
    source = f"""
{_extract_js_function(UI_JS, '_activeRunScopeQuery')}
{_extract_js_function(UI_JS, '_renderActiveRunVisibility')}
{_extract_js_function(UI_JS, 'refreshActiveRunVisibility')}
{_extract_js_function(UI_JS, '_invalidateActiveRunVisibilityScope')}
let _activeRunSnapshot = {{runs: []}};
let _activeRunSnapshotInflight = null;
let _activeRunSnapshotRefreshQueued = false;
let _activeRunSnapshotRequestSeq = 0;
let _activeRunSnapshotFreshAt = 0;
const ACTIVE_RUN_SNAPSHOT_STALE_MS = 15000;
global.$ = () => null;
global._activeProject = 'old-project';
global._showAllProfiles = false;
global._requestedSessionSidebarSource = () => 'webui';
global._sessionListExcludeHiddenEnabled = () => false;
const requests = [];
global.api = (url) => new Promise(resolve => requests.push({{url, resolve}}));

async function main() {{
  const oldRequest = refreshActiveRunVisibility();
  _activeProject = 'new-project';
  _invalidateActiveRunVisibilityScope();
  requests[0].resolve({{runs: [{{session_id: 'old-run'}}]}});
  await oldRequest;
  await Promise.resolve();
  const newRequest = requests[1];
  newRequest.resolve({{runs: [{{session_id: 'new-run'}}]}});
  await newRequest;
  console.log(JSON.stringify({{
    requestUrls: requests.map(request => request.url),
    sessionIds: _activeRunSnapshot.runs.map(run => run.session_id),
  }}));
}}
main().catch(error => {{ console.error(error.stack || error); process.exit(1); }});
"""
    assert _run_node(source) == {
        "requestUrls": [
            "/api/activity/active-runs?sidebar_source=webui&project_id=old-project",
            "/api/activity/active-runs?sidebar_source=webui&project_id=new-project",
        ],
        "sessionIds": ["new-run"],
    }


def test_active_run_scope_mutators_clear_old_projection_and_queue_latest_scope():
    source = f"""
{_extract_js_function(UI_JS, '_activeRunScopeQuery')}
{_extract_js_function(UI_JS, '_renderActiveRunVisibility')}
{_extract_js_function(UI_JS, 'refreshActiveRunVisibility')}
{_extract_js_function(UI_JS, '_invalidateActiveRunVisibilityScope')}
{_extract_js_function(SESSIONS_JS, '_requestedSessionSidebarSource')}
{_extract_js_function(SESSIONS_JS, '_sessionListExcludeHiddenEnabled')}
{_extract_js_function(SESSIONS_JS, '_setActiveProjectFilter')}
{_extract_js_function(SESSIONS_JS, '_setSessionSourceFilter')}
{_extract_js_block(SESSIONS_JS, 'async function _handleShowAllProfilesStorageEvent(')}
{_extract_js_function(PANELS_JS, '_applySavedSettingsUi')}
const NO_PROJECT_FILTER = '__none__';
const SHOW_ALL_PROFILES_STORAGE_KEY = 'hermes-show-all-profiles';
let _activeRunSnapshot = {{runs: []}};
let _activeRunSnapshotInflight = null;
let _activeRunSnapshotRefreshQueued = false;
let _activeRunSnapshotRequestSeq = 0;
let _activeRunSnapshotFreshAt = 0;
const ACTIVE_RUN_SNAPSHOT_STALE_MS = 15000;
global.window = global;
global.document = {{documentElement: {{dataset: {{}}}}}};
global.$ = () => null;
global.URLSearchParams = URLSearchParams;
global.localStorage = {{setItem() {{}}, getItem() {{ return null; }}}};
global._selectedSessions = new Set();
global._sessionSelectMode = false;
global._showArchived = false;
global._activeProject = 'project-1';
global._showAllProfiles = false;
global._sessionSourceFilter = 'cli';
global._showCliSessions = true;
global._showTokenUsage = false;
global._showQuotaChip = false;
global._showConversationOutline = false;
global._showBusyPlaceholderHint = false;
global._showTps = false;
global._fadeTextEffect = false;
global.renderSessionListFromCache = () => {{}};
global.renderSessionList = async () => {{}};
global.refreshSessionList = async () => {{}};
global._syncChatActivityDisplayModeControl = () => {{}};
global._syncTransparentEventTimestampsControl = () => {{}};
global._applyWorkspaceTodosTabVisibility = () => {{}};
global._applySessionNavigationPrefs = () => {{}};
global._persistDefaultMessageMode = value => value;
global._applyStructuredCodeViewSettings = () => {{}};
global.applyBotName = () => {{}};
global.setLocale = () => {{}};
global.applyLocaleToDOM = () => {{}};
global._ensureComposerControlVisibilityState = () => {{}};
global._renderComposerControlChips = () => {{}};
global._renderComposerSituationalControlChips = () => {{}};
global._applyComposerFooterVisibilitySettings = () => {{}};
global._applyComposerControlOrder = () => {{}};
global._setComposerControlOrder = value => value;
global.startGatewaySSE = () => {{}};
global.stopGatewaySSE = () => {{}};
global._syncSettingsMaxTokensPlaceholder = () => {{}};
global._invalidateComposerReasoningContext = () => {{}};
global._setSettingsAuthButtonsVisible = () => {{}};
global.clearMessageRenderCache = () => {{}};
global.renderMessages = () => {{}};
global.syncTopbar = () => {{}};
global._settingsHermesDefaultModelOnOpen = '';
global._settingsHermesDefaultModelProviderOnOpen = null;
const requests = [];
global.api = (url) => {{
  let resolve;
  const promise = new Promise(r => {{ resolve = r; }});
  requests.push({{url, resolve, promise}});
  return promise;
}};

async function runMutation(label, mutate) {{
  requests.length = 0;
  _activeProject = 'project-1';
  _showAllProfiles = false;
  _sessionSourceFilter = 'cli';
  window._showCliSessions = true;
  _activeRunSnapshot = {{runs: [{{session_id: 'persisted'}}]}};
  _activeRunSnapshotFreshAt = 1;
  _activeRunSnapshotInflight = null;
  _activeRunSnapshotRefreshQueued = false;
  _activeRunSnapshotRequestSeq = 0;
  const oldRequest = refreshActiveRunVisibility();
  await Promise.resolve();
  await mutate();
  const cleared = Array.isArray(_activeRunSnapshot.runs) && _activeRunSnapshot.runs.length === 0;
  requests[0].resolve({{runs: [{{session_id: 'stale'}}]}});
  await oldRequest;
  await Promise.resolve();
  requests[1].resolve({{runs: [{{session_id: label}}]}});
  await requests[1].promise;
  await Promise.resolve();
  return {{
    urls: requests.map(request => request.url),
    cleared,
    sessionIds: _activeRunSnapshot.runs.map(run => run.session_id),
  }};
}}

async function main() {{
  const results = {{
    project: await runMutation('project-run', () => _setActiveProjectFilter('project-2')),
    source: await runMutation('source-run', () => _setSessionSourceFilter('webui')),
    allProfiles: await runMutation('all-profiles-run', () => _handleShowAllProfilesStorageEvent({{
      key: 'hermes-show-all-profiles',
      newValue: 'true',
    }})),
    cliSetting: await runMutation('cli-setting-run', () => _applySavedSettingsUi(
      {{}},
      {{
        show_previous_messaging_sessions: false,
        sound_enabled: false,
        notifications_enabled: false,
        whats_new_summary_enabled: false,
        show_thinking: true,
        chat_activity_display_mode: null,
        transparent_stream_event_timestamps: false,
        terminal_auto_expand_on_output: false,
        workspace_todos_tab: false,
        session_jump_buttons: false,
        default_message_mode: 'steer',
        session_endless_scroll: false,
        auto_scroll_follow: true,
        large_text_paste_as_attachment: true,
        project_quick_create_buttons: false,
        bot_name: 'Hermes',
      }},
      {{
        sendKey: 'enter',
        showTokenUsage: false,
        showQuotaChip: false,
        showConversationOutline: false,
        showBusyPlaceholderHint: false,
        showTps: false,
        fadeTextEffect: false,
        showCliSessions: false,
        theme: 'dark',
        skin: 'default',
        language: 'en',
        sidebarDensity: 'compact',
        fontSize: '14',
      }},
    )),
  }};
  console.log(JSON.stringify(results));
}}
main().catch(error => {{ console.error(error.stack || error); process.exit(1); }});
"""
    assert _run_node(source) == {
        "project": {
            "urls": [
                "/api/activity/active-runs?sidebar_source=cli&project_id=project-1",
                "/api/activity/active-runs?sidebar_source=cli&project_id=project-2",
            ],
            "cleared": True,
            "sessionIds": ["project-run"],
        },
        "source": {
            "urls": [
                "/api/activity/active-runs?sidebar_source=cli&project_id=project-1",
                "/api/activity/active-runs?sidebar_source=webui&exclude_hidden=1",
            ],
            "cleared": True,
            "sessionIds": ["source-run"],
        },
        "allProfiles": {
            "urls": [
                "/api/activity/active-runs?sidebar_source=cli&project_id=project-1",
                "/api/activity/active-runs?sidebar_source=cli&project_id=project-1&all_profiles=1",
            ],
            "cleared": True,
            "sessionIds": ["all-profiles-run"],
        },
        "cliSetting": {
            "urls": [
                "/api/activity/active-runs?sidebar_source=cli&project_id=project-1",
                "/api/activity/active-runs?sidebar_source=webui&project_id=project-1",
            ],
            "cleared": True,
            "sessionIds": ["cli-setting-run"],
        },
    }


def test_active_run_visibility_document_handlers_close_tray_on_outside_click_and_escape():
    source = f"""
{_extract_js_function(UI_JS, '_hideActiveRunTray')}
{_extract_js_function(UI_JS, '_toggleActiveRunTray')}
function makeNode(tag) {{
  return {{
    tagName: String(tag || '').toUpperCase(),
    children: [],
    hidden: false,
    textContent: '',
    parentNode: null,
    attributes: {{}},
    listeners: {{}},
    appendChild(child) {{
      child.parentNode = this;
      this.children.push(child);
      return child;
    }},
    addEventListener(type, handler) {{
      this.listeners[type] = handler;
    }},
    setAttribute(name, value) {{
      this.attributes[name] = String(value);
    }},
    getAttribute(name) {{
      return Object.prototype.hasOwnProperty.call(this.attributes, name)
        ? this.attributes[name]
        : null;
    }},
    contains(target) {{
      if (this === target) return true;
      return this.children.some(child => typeof child.contains === 'function' && child.contains(target));
    }},
    focus() {{
      this.focused = true;
    }},
  }};
}}

const documentListeners = {{}};
const elements = {{
  activeRunVisibility: makeNode('div'),
  activeRunPill: makeNode('button'),
  activeRunTray: makeNode('div'),
}};
elements.activeRunVisibility.appendChild(elements.activeRunPill);
elements.activeRunVisibility.appendChild(elements.activeRunTray);
function $(id) {{
  return elements[id] || null;
}}

let refreshCalls = 0;
global.document = {{
  addEventListener(type, handler) {{
    (documentListeners[type] ||= []).push(handler);
  }},
  createElement: makeNode,
}};
global.setInterval = () => 1;
global.refreshActiveRunVisibility = async () => {{ refreshCalls += 1; }};
{_extract_js_block(UI_JS, "document.addEventListener('DOMContentLoaded', () =>")});

async function main() {{
  for (const handler of documentListeners.DOMContentLoaded || []) {{
    await handler();
  }}
  elements.activeRunPill.textContent = '1 active';
  elements.activeRunTray.hidden = true;
  elements.activeRunPill.listeners.click();
  const expandedBeforeClose = elements.activeRunPill.getAttribute('aria-expanded');

  const outsideTarget = makeNode('div');
  for (const handler of documentListeners.click || []) {{
    handler({{target: outsideTarget}});
  }}
  const trayHiddenAfterOutside = elements.activeRunTray.hidden;
  const expandedAfterOutside = elements.activeRunPill.getAttribute('aria-expanded');

  elements.activeRunTray.hidden = false;
  elements.activeRunPill.focused = false;
  for (const handler of documentListeners.keydown || []) {{
    handler({{key: 'Escape'}});
  }}
  const focusRestoredAfterEscape = elements.activeRunPill.focused === true;

  elements.activeRunPill.focused = false;
  for (const handler of documentListeners.keydown || []) {{
    handler({{key: 'Escape'}});
  }}

  console.log(JSON.stringify({{
    refreshCalls,
    expandedBeforeClose,
    trayHiddenAfterOutside,
    expandedAfterOutside,
    trayHiddenAfterEscape: elements.activeRunTray.hidden,
    focusRestoredAfterEscape,
    focusPreservedWhenAlreadyClosed: elements.activeRunPill.focused === false,
  }}));
}}

main().catch(err => {{
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
}});
"""
    assert _run_node(source) == {
        "refreshCalls": 1,
        "expandedBeforeClose": "true",
        "trayHiddenAfterOutside": True,
        "expandedAfterOutside": "false",
        "trayHiddenAfterEscape": True,
        "focusRestoredAfterEscape": True,
        "focusPreservedWhenAlreadyClosed": True,
    }


def test_active_run_profile_and_source_coercion_paths_call_scope_invalidator():
    switch_profile_for_load = _extract_js_function(SESSIONS_JS, "_switchProfileForSessionLoad")
    switch_to_profile = _extract_js_function(PANELS_JS, "switchToProfile")
    assert "_invalidateActiveRunVisibilityScope()" in switch_profile_for_load
    assert "_invalidateActiveRunVisibilityScope()" in switch_to_profile
    assert "if(_sessionSourceFilter==='cli'){" in SESSIONS_JS
    assert "if(typeof _invalidateActiveRunVisibilityScope==='function') _invalidateActiveRunVisibilityScope();" in SESSIONS_JS


def test_active_run_lights_only_existing_sidebar_ring():
    assert "function _isSessionRingStreaming(s)" in SESSIONS_JS
    shared = SESSIONS_JS[
        SESSIONS_JS.index("function _isSessionEffectivelyStreaming(s)"):SESSIONS_JS.index(
            "function _isSessionRingStreaming(s)"
        )
    ]
    assert "_activeRunSessionIds" not in shared
    assert "_activeRunSessionIds.has(s.session_id)" in SESSIONS_JS
    assert "const ownRingStreaming=_isSessionRingStreaming(s);" in SESSIONS_JS
    assert "session-state-indicator" in SESSIONS_JS
    assert "_reconcileActiveSessionIdleStateFromList" in SESSIONS_JS
    assert "_activeRunSessionIds.clear()" in UI_JS


def test_active_run_only_row_renders_ring_without_lifecycle_state_or_unread():
    source = f"""
{_extract_js_function(SESSIONS_JS, '_isSessionLocallyStreaming')}
{_extract_js_function(SESSIONS_JS, '_hasPendingUserMessageSignal')}
{_extract_js_function(SESSIONS_JS, '_isSessionEffectivelyStreaming')}
{_extract_js_function(SESSIONS_JS, '_isSessionRingStreaming')}
{_extract_js_function(SESSIONS_JS, '_rememberRenderedStreamingState')}
{_extract_js_function(SESSIONS_JS, '_sessionRunningSortRank')}
{_extract_js_function(SESSIONS_JS, '_markPollingCompletionUnreadTransitions')}
{_render_one_session_prefix(SESSIONS_JS)}

function makeElement() {{
  return {{
    className: '',
    children: [],
    dataset: {{}},
    style: {{}},
    classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
    appendChild(child) {{ this.children.push(child); return child; }},
    append(...children) {{ this.children.push(...children); }},
    addEventListener() {{}},
    setAttribute() {{}},
  }};
}}

const remembered = new Map();
let completionUnreadCalls = 0;
global.S = {{session: null, busy: false}};
global.activeSidForSidebar = null;
global._activeRunSessionIds = new Set(['active-run-only']);
global._sessionStreamingById = remembered;
global._sessionListSnapshotById = new Map();
global._sessionListSourceById = new Map();
global._allSessionsScope = {{}};
global._sessionLineageContainsSession = () => false;
global._hasUnreadForSession = () => false;
global._sessionAttentionState = () => null;
global._isReadOnlySession = () => false;
global._rememberRenderedSessionSnapshot = () => {{}};
global._rememberObservedStreamingSession = () => {{}};
global._rememberSessionListSource = () => {{}};
global._getSessionObservedStreaming = () => ({{}});
global._isSessionActivelyViewedForList = () => false;
global._setSessionViewedCount = () => {{}};
global._forgetObservedStreamingSession = () => {{}};
global._markSessionCompletionUnread = () => {{ completionUnreadCalls += 1; }};
global.document = {{createElement: makeElement}};

const row = {{session_id: 'active-run-only', message_count: 0}};
const active = _renderOneSession(row);
const activeState = {{
  ring: active.className.includes(' streaming'),
  shared: _isSessionEffectivelyStreaming(row),
  rank: _sessionRunningSortRank(row),
  remembered: remembered.get(row.session_id) === true,
}};
const childOnlyParent = _renderOneSession({{
  session_id: 'child-only-parent',
  message_count: 0,
  _child_session_streaming: true,
}});
_markPollingCompletionUnreadTransitions([row]);
_activeRunSessionIds.clear();
const idle = _renderOneSession(row);
_markPollingCompletionUnreadTransitions([row]);
console.log(JSON.stringify({{
  active: activeState,
  childOnlyParent: {{
    ring: childOnlyParent.className.includes(' streaming'),
    shared: _isSessionEffectivelyStreaming({{session_id: 'child-only-parent', message_count: 0}}),
  }},
  idle: {{
    ring: idle.className.includes(' streaming'),
    shared: _isSessionEffectivelyStreaming(row),
    rank: _sessionRunningSortRank(row),
    remembered: remembered.get(row.session_id) === true,
    completionUnreadCalls,
  }},
}}));
"""
    result = _run_node(source)
    assert result["active"] == {"ring": True, "shared": False, "rank": 0, "remembered": False}
    assert result["childOnlyParent"] == {"ring": False, "shared": False}
    assert result["idle"] == {
        "ring": False,
        "shared": False,
        "rank": 0,
        "remembered": False,
        "completionUnreadCalls": 0,
    }


def test_active_run_only_child_and_fork_child_render_rings_without_lifecycle_state():
    fork_loop = _extract_js_block(
        SESSIONS_JS,
        "for(const child of sortedChildren){",
        SESSIONS_JS.index("const childList=document.createElement('div');"),
    )
    source = f"""
{_extract_js_function(SESSIONS_JS, '_isSessionLocallyStreaming')}
{_extract_js_function(SESSIONS_JS, '_hasPendingUserMessageSignal')}
{_extract_js_function(SESSIONS_JS, '_isSessionEffectivelyStreaming')}
{_extract_js_function(SESSIONS_JS, '_isSessionRingStreaming')}
{_extract_js_function(SESSIONS_JS, '_sessionRunningSortRank')}
{_extract_js_function(SESSIONS_JS, '_isChildSession')}
{_extract_js_function(SESSIONS_JS, '_isForkWithResolvableParent')}
{_extract_js_function(SESSIONS_JS, '_sidebarLineageKeyForRow')}
{_extract_js_function(SESSIONS_JS, '_attachChildSessionsToSidebarRows')}

function element() {{
  return {{
    className: '',
    children: [],
    dataset: {{}},
    style: {{}},
    classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
    appendChild(child) {{ this.children.push(child); return child; }},
    append(...children) {{ this.children.push(...children); }},
    addEventListener() {{}},
    setAttribute() {{}},
  }};
}}

function runForkLoop(sortedChildren, childList, childLabelFor, openChildSession, installForkChildSwipe, _buildSessionRenameStarter) {{
  {fork_loop}
}}

global.S = {{session: null, busy: false}};
global._activeRunSessionIds = new Set(['active-child', 'active-fork']);
global._showArchived = false;
global._hasUnreadForSession = () => false;
global._isExternalSession = () => false;
global._isMessagingSession = () => false;
global._isReadOnlySession = () => true;
global._sessionTimestampMs = () => 0;
global._sessionDisplayTitle = (s) => s.title || 'Untitled';
global._formatRelativeSessionTime = () => 'now';
global._sessionAttentionState = () => null;
global._sessionStateTooltip = () => '';
global._sessionLineageContainsSession = () => false;
global.activeSidForSidebar = null;
global._sessionSelectMode = false;
global.document = {{createElement: element}};

const child = {{session_id: 'active-child', parent_session_id: 'parent', relationship_type: 'child_session', title: 'Child'}};
const parent = {{session_id: 'parent', title: 'Parent'}};
const attached = _attachChildSessionsToSidebarRows([parent], [parent, child]);
const attachedParent = attached[0];
const sortedChildren = [{{session_id: 'active-fork', session_source: 'fork', title: 'Fork child'}}];
const childList = document.createElement('div');
const childLabelFor = (child) => child.title;
const openChildSession = async () => {{}};
const installForkChildSwipe = () => {{}};
const _buildSessionRenameStarter = () => () => {{}};
runForkLoop(sortedChildren, childList, childLabelFor, openChildSession, installForkChildSwipe, _buildSessionRenameStarter);
const forkRow = childList.children[0];

console.log(JSON.stringify({{
  childRing: attachedParent._child_session_streaming === true,
  childShared: _isSessionEffectivelyStreaming(child),
  childRank: _sessionRunningSortRank(child),
  childFastPoll: [child].some(_isSessionEffectivelyStreaming),
  forkRing: forkRow.className.includes(' streaming') && forkRow.children[1].className.includes(' is-streaming'),
  forkShared: _isSessionEffectivelyStreaming(sortedChildren[0]),
  forkRank: _sessionRunningSortRank(sortedChildren[0]),
  forkFastPoll: sortedChildren.some(_isSessionEffectivelyStreaming),
}}));
"""
    assert _run_node(source) == {
        "childRing": True,
        "childShared": False,
        "childRank": 0,
        "childFastPoll": False,
        "forkRing": True,
        "forkShared": False,
        "forkRank": 0,
        "forkFastPoll": False,
    }
