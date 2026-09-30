"""Production-composed regressions for New Chat navigation supersession."""

from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
MESSAGES_JS = ROOT / "static" / "messages.js"
WORKSPACE_JS = ROOT / "static" / "workspace.js"
PANELS_JS = ROOT / "static" / "panels.js"
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
_AUTHORITY_START = UI_JS.index("let _composerOwnershipTransition=null;")
_AUTHORITY_END = UI_JS.index("const OFFLINE_RECHECK_MS", _AUTHORITY_START)
COMPOSER_AUTHORITY_JS = UI_JS[_AUTHORITY_START:_AUTHORITY_END]


_BOOTSTRAP = r"""
(() => {
  document.body.innerHTML = `
    <div id="msgInner"></div>
    <textarea id="msg"></textarea>
    <select id="modelSelect"><option value="test-model" selected>test-model</option></select>
    <div id="composerStatus"></div><div id="statusText"></div>
    <div id="attachTray"></div><div id="emptyState"></div>
    <button id="btnNewChat"></button><button id="btnTitlebarNewChat"></button>
    <input id="fileInput"><button id="btnAttach"></button><button id="btnSavedPrompts"></button>
    <button id="btnMic"></button><button id="btnVoiceMode"></button>
    <div id="a11yAnnouncer"></div><div id="composerWorkspaceContext"></div>
  `;
  window.S = {
    session: null, messages: [], entries: [], busy: false, pendingFiles: [], toolCalls: [],
    activeStreamId: null, currentDir: '.', activeProfile: 'default', activeProfileIsDefault: true,
    showHiddenWorkspaceFiles: false, todos: [], todoStateMeta: null, _pendingSessionToolsets: null,
    _profileDefaultWorkspace: '/workspace-a', _profileSwitchWorkspace: null, lastUsage: {}, _dirCache: {},
  };
  window.INFLIGHT = {};
  window.SESSION_QUEUES = {};
  window._activeProject = null;
  window.NO_PROJECT_FILTER = '__all_projects__';
  window._sessionSourceFilter = 'webui';
  window._allSessions = [];
  window._allSessionsScope = {};
  window._profilesCache = {profiles: []};
  window._defaultModel = null;
  window._activeProvider = null;
  window._pendingSessionToolsets = null;
  window._forcedSkillDirectivePending = null;
  window.__apiLog = [];
  window.__pendingRequests = [];
  window.__commandCalls = [];
  window.__actionError = null;
  window.__navigationError = null;
  window.__refreshError = null;
  window.__refreshSettled = false;
  window.__rememberedNewChatDraft = null;
  try { Object.defineProperty(document, 'hidden', {configurable: true, value: false}); } catch (_) {}

  window.__requestKind = path => {
    if (path === '/api/session/new') return 'new';
    if (path === '/api/chat/start') return 'chat-start';
    if (path === '/api/session/draft') return 'draft';
    if (path.includes('/api/session?') && path.includes('messages=0')) return 'metadata';
    if (path.includes('/api/session?') && path.includes('messages=1')) return 'messages';
    return 'other';
  };
  window.__requestSid = (path, body) => {
    if (body && body.session_id) return String(body.session_id);
    try { return new URL(path, 'http://local.test').searchParams.get('session_id'); }
    catch (_) { return null; }
  };
  window.__pendingRequest = (kind, sid = null, ordinal = 0) => {
    const matches = window.__pendingRequests.filter(request =>
      !request.settled && request.kind === kind && (sid === null || request.sid === sid)
    );
    return matches[ordinal] || null;
  };
  window.__resolveRequest = (kind, sid, payload, ordinal = 0) => {
    const request = window.__pendingRequest(kind, sid, ordinal);
    if (!request) throw new Error(`missing pending ${kind} request for ${sid}`);
    request.settled = true;
    request.resolve(payload);
  };
  window.api = (url, options = {}) => {
    const path = String(url);
    const method = String(options.method || 'GET');
    let body = null;
    try { body = options.body ? JSON.parse(options.body) : null; }
    catch (_) { body = options.body || null; }
    const kind = window.__requestKind(path);
    const sid = window.__requestSid(path, body);
    window.__apiLog.push({path, method, body, kind, sid, visibleSid: S.session && S.session.session_id});
    if (kind === 'chat-start') return Promise.resolve({stream_id: 'stream-test'});
    if (kind === 'draft' && !window.__holdDraft) return Promise.resolve({});
    if (path.startsWith('/api/list?')) {
      if (window.__holdWorkspace && sid === 'session-a') {
        return new Promise(resolve => { window.__releaseWorkspace = () => resolve({entries: [{name: 'A.txt'}]}); });
      }
      return Promise.resolve({entries: [{name: sid === 'session-b' ? 'B.txt' : 'A.txt'}]});
    }
    if (path === '/api/profile/switch') {
      if (window.__holdProfile && body.name === 'other') {
        return new Promise((resolve, reject) => {
          window.__pendingRequests.push({kind: 'profile', sid: null, resolve, reject, settled: false});
        });
      }
      return Promise.resolve({active: body.name, is_default: false});
    }
    if (path.startsWith('/api/sessions')) return Promise.resolve({sessions: []});
    if (kind === 'other') return Promise.resolve({});
    return new Promise((resolve, reject) => {
      window.__pendingRequests.push({kind, sid, path, resolve, reject, settled: false});
    });
  };
  window.t = key => key;
  window.$ = id => document.getElementById(id);
  window.getModelLabel = model => String(model || '');
})();
"""


_OVERRIDES = r"""
(() => {
  const noops = [
    '_rearmActiveSessionStream', 'stopApprovalPolling', 'hideApprovalCard', 'stopSessionStream',
    '_updateYoloPill', 'stopClarifyPolling', 'hideClarifyCard', '_captureSameSessionForceReloadHint',
    '_clearSameSessionForceReloadHint', '_uploadPendingFilesSyncProgressForSession', '_clearPendingSelections',
    '_clearQueueCardDisplay', '_hydrateTodosFromSession', '_resolveSessionModelForDisplaySoon',
    '_setActiveSessionUrl', 'startSessionStream', 'clearLiveToolCards', 'updateQueueBadge', 'setStatus',
    'setComposerStatus', 'updateSendBtn', 'renderMessages', 'renderSessionArtifacts',
    '_renderPendingPromptsForActiveSession', '_hideHandoffHint', '_checkAndShowHandoffHint',
    '_deferWorkspaceRefreshForSession', '_fetchYoloState', 'startApprovalPolling', 'startClarifyPolling',
    'resumeManualCompressionForSession', '_acknowledgeSessionVisit', '_clearDeferredActiveSessionExternalRefresh',
    '_clearEmptyComposerModelOverride', 'populateModelDropdown',
    'refreshSessionList', 'loadDir', 'clearCompressionUi', 'renderSessionListFromCache',
    'renderSessionList', 'syncTopbar', 'autoResize', 'renderTray', 'applySessionTitleUpdate',
    'upsertActiveSessionForLocalTurn', 'ensureLiveWorklogShell', 'appendThinking', 'markInflight',
    'saveInflightState', 'attachLiveStream', '_dismissHandoffHint', 'hideCmdDropdown',
  ];
  for (const name of noops) window[name] = () => {};
  const rememberNewChatDraftSession = window._rememberNewChatDraftSession;
  window._rememberNewChatDraftSession = session => {
    window.__rememberedNewChatDraft = JSON.parse(JSON.stringify(session));
    return rememberNewChatDraftSession(session);
  };
  window._deferSessionSideEffect = (_sid, fn) => Promise.resolve(fn());
  window._resolveSessionIdFromSidebarLineage = sid => sid;
  window._isMessageReaderUnpinned = () => false;
  window._isSessionActivelyViewedForList = () => true;
  window._isExternalSession = () => false;
  window._readEmptyComposerModelOverride = () => null;
  window._readPersistedModelState = () => null;
  window._modelStateForSelect = (_select, value) => ({model: value, model_provider: null});
  window._modelProviderForSend = () => null;
  window._applyModelToDropdown = () => true;
  window._messageReloadLimitForSession = () => 2;
  window._currentMessageRenderWindowSize = () => 1;
  window._messageRenderableMessageCount = () => 1;
  window._msgLimitMax = 500;
  window._MSG_LIMIT_MAX = 500;
  window.uploadPendingFiles = () => Promise.resolve([]);
  window.setBusy = value => { S.busy = !!value; };
  window._flushSelectionBlocksToComposer = () => {};
  window._composerTextWithPendingSelections = () => document.getElementById('msg').value;
  window.shouldInterceptCompressionRecoveryContinuation = () => false;
  window._runOptionalPreStartUiStep = (_name, fn) => { try { return fn(); } catch (_) {} };
  window._runOptionalPostStartUiStep = (_name, fn) => { try { return fn(); } catch (_) {} };
  window.parseCommand = text => {
    if (!String(text).startsWith('/local')) return null;
    return {name: 'local', args: String(text).slice('/local'.length).trim()};
  };
  window.COMMANDS = [{
    name: 'local', noEcho: false,
    fn(args) { window.__commandCalls.push({args, sid: S.session && S.session.session_id}); }
  }];
})();
"""


def _settle_cross_session_navigation(page, *, created_sid: str = "session-a") -> None:
    page.evaluate(
        """
        sid => window.__resolveRequest('new', null, {session: {
          session_id: sid, profile: 'default', workspace: '/workspace-a', title: 'Untitled',
          message_count: 0, messages: [], composer_draft: {text: '', files: []}, model: 'test-model'
        }})
        """,
        created_sid,
    )
    page.wait_for_function(
        """
        () => !!window.__pendingRequest('metadata', 'session-b')
          || !!window.__actionError || !!window.__navigationError
        """
    )
    diagnostic = page.evaluate(
        """
        () => ({
          actionError: window.__actionError,
          navigationError: window.__navigationError,
          visibleSid: S.session && S.session.session_id,
          text: document.getElementById('msg').value,
          sendInProgress: _sendInProgress,
          hasNewSessionRequest: _newSessionRequest !== null,
          hasNewSessionInFlight: _newSessionInFlight !== null,
          apiLog: window.__apiLog,
          pending: window.__pendingRequests.map(request => ({
            kind: request.kind, sid: request.sid, settled: request.settled
          })),
        })
        """
    )
    assert diagnostic["actionError"] is None, diagnostic
    assert diagnostic["navigationError"] is None, diagnostic
    assert page.evaluate("() => !!window.__pendingRequest('metadata', 'session-b')"), diagnostic
    page.evaluate(
        """
        () => window.__resolveRequest('metadata', 'session-b', {session: {
          session_id: 'session-b', profile: 'default', workspace: '/workspace-b', title: 'B',
          message_count: 1, messages: [], composer_draft: {text: 'draft B', files: []},
          active_stream_id: null
        }})
        """
    )
    page.wait_for_function("() => !!window.__pendingRequest('messages', 'session-b')")
    page.evaluate(
        """
        () => window.__resolveRequest('messages', 'session-b', {session: {
          session_id: 'session-b', profile: 'default', workspace: '/workspace-b', title: 'B',
          message_count: 1, messages: [{role: 'assistant', content: 'B transcript'}],
          tool_calls: [], _messages_truncated: false, _messages_offset: 0
        }})
        """
    )
    page.evaluate("() => Promise.all([window.__actionDone, window.__navigationDone])")


@pytest.fixture()
def production_page():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("http://local.test/", lambda route: route.fulfill(body="<!doctype html><html><body></body></html>", content_type="text/html"))
        page.goto("http://local.test/")
        page.evaluate(_BOOTSTRAP)
        page.add_script_tag(content=COMPOSER_AUTHORITY_JS)
        page.evaluate("() => { window.__fixtureApi = api; }")
        page.add_script_tag(path=str(WORKSPACE_JS))
        page.evaluate("() => { window.__productionLoadDir = loadDir; window.api = window.__fixtureApi; }")
        page.add_script_tag(path=str(PANELS_JS))
        page.add_script_tag(path=str(SESSIONS_JS))
        page.add_script_tag(path=str(MESSAGES_JS))
        page.evaluate(_OVERRIDES)
        page.evaluate(r'''() => {
          for (const name of ['_restoreExpandedDirs', 'renderBreadcrumb', 'renderFileTree',
              '_refreshGitBadge', 'clearPreview', '_syncWorkspaceBirthtimeSupportScope',
              '_profileSwitchPanelLoad', '_refreshProfileSwitchBackground', '_clearPendingSkill',
              'closeSessionActionMenu', 'showSessionListSkeleton', '_invalidateSessionListRenders']) {
            window[name] = () => {};
          }
          window._workspaceRouteForPath = () => null;
          window.__toasts = [];
          window.showToast = (...args) => window.__toasts.push(args);
          window.__enableWorkspace = () => {
            window.loadDir = window.__productionLoadDir;
            window._deferWorkspaceRefreshForSession = () => loadDir('.');
          };
        }''')
        yield page, errors
        page.close()
        browser.close()


def _start_action_then_navigation(
    page, *, text: str, with_file: bool, late_text: str = ""
) -> None:
    page.evaluate(
        """
        ({text, withFile}) => {
          const msg = document.getElementById('msg');
          msg.value = text;
          const destinationFile = new File(['destination'], 'session-b.txt', {type: 'text/plain'});
          _rememberComposerPendingFiles('session-b', [destinationFile], 'default');
          S.pendingFiles = withFile
            ? [new File(['source'], 'first-send.txt', {type: 'text/plain'})]
            : [];
          window.__actionDone = send().catch(error => {
            window.__actionError = String(error && error.stack || error);
          });
        }
        """,
        {"text": text, "withFile": with_file},
    )
    page.wait_for_function("() => !!window.__pendingRequest('new')")
    page.evaluate(
        """
        () => {
          window.__navigationDone = loadSession('session-b', {force: true}).catch(error => {
            window.__navigationError = String(error && error.stack || error);
          });
        }
        """
    )
    if late_text:
        page.evaluate("value => _composerAppendText(value)", late_text)
    page.evaluate("() => Promise.resolve().then(() => Promise.resolve())")


@pytest.mark.parametrize(
    ("text", "late_text", "expected_draft_text"),
    [
        pytest.param(
            "first-send payload",
            " + late producer",
            "first-send payload + late producer",
            id="existing-text",
        ),
        pytest.param("", "late producer", "late producer", id="append-only"),
    ],
)
def test_first_send_stops_when_newer_navigation_supersedes_implicit_creation(
    production_page, text, late_text, expected_draft_text
):
    page, errors = production_page
    _start_action_then_navigation(
        page,
        text=text,
        with_file=True,
        late_text=late_text,
    )

    assert page.evaluate("() => window.__apiLog.some(call => call.kind === 'metadata' && call.sid === 'session-b')") is False
    _settle_cross_session_navigation(page)

    observed = page.evaluate(
        """
        () => ({
          finalSid: S.session && S.session.session_id,
          text: document.getElementById('msg').value,
          files: S.pendingFiles.map(file => file.name),
          messages: S.messages.map(message => ({role: message.role, content: message.content})),
          chatStarts: window.__apiLog.filter(call => call.kind === 'chat-start'),
          draftWrites: window.__apiLog.filter(call => call.kind === 'draft'),
          sendInProgress: _sendInProgress,
          newSessionPending: _newSessionInFlight !== null,
          rememberedNewChatDraft: window.__rememberedNewChatDraft,
        })
        """
    )

    assert observed["chatStarts"] == [], "superseded first Send must not start a turn in session A or B"
    assert observed["finalSid"] == "session-b"
    assert observed["text"] == "draft B"
    assert observed["files"] == ["session-b.txt"]
    assert observed["messages"] == [{"role": "assistant", "content": "B transcript"}]
    assert any(
        write["body"]["session_id"] == "session-a"
        and write["body"]["text"] == expected_draft_text
        and [file["name"] for file in write["body"].get("files", [])] == ["first-send.txt"]
        for write in observed["draftWrites"]
    ), "the cancelled turn must remain recoverable as session A's draft"
    assert observed["rememberedNewChatDraft"]["session_id"] == "session-a"
    assert (
        observed["rememberedNewChatDraft"]["composer_draft"]["text"]
        == expected_draft_text
    )
    assert [
        file["name"] for file in observed["rememberedNewChatDraft"]["composer_draft"]["files"]
    ] == ["first-send.txt"]
    assert observed["sendInProgress"] is False
    assert observed["newSessionPending"] is False
    assert errors == []


def test_local_command_stops_when_newer_navigation_supersedes_implicit_creation(production_page):
    page, errors = production_page
    _start_action_then_navigation(page, text="/local keep-this", with_file=False)
    _settle_cross_session_navigation(page)

    observed = page.evaluate(
        """
        () => ({
          finalSid: S.session && S.session.session_id,
          text: document.getElementById('msg').value,
          files: S.pendingFiles.map(file => file.name),
          messages: S.messages.map(message => ({role: message.role, content: message.content})),
          commandCalls: window.__commandCalls,
          chatStarts: window.__apiLog.filter(call => call.kind === 'chat-start'),
          draftWrites: window.__apiLog.filter(call => call.kind === 'draft'),
        })
        """
    )

    assert observed["commandCalls"] == [], "a superseded command must not execute against session A or B"
    assert observed["chatStarts"] == []
    assert observed["finalSid"] == "session-b"
    assert observed["text"] == "draft B"
    assert observed["files"] == ["session-b.txt"]
    assert observed["messages"] == [{"role": "assistant", "content": "B transcript"}]
    assert any(
        write["body"]["session_id"] == "session-a"
        and write["body"]["text"] == "/local keep-this"
        for write in observed["draftWrites"]
    ), "the cancelled command must remain recoverable as session A's draft"
    assert errors == []


def test_automatic_same_session_refresh_does_not_supersede_new_chat(production_page):
    page, errors = production_page
    page.evaluate(
        """
        () => {
          S.session = {
            session_id: 'session-old', profile: 'default', workspace: '/workspace-old', title: 'Old',
            message_count: 1, updated_at: 1, last_message_at: 1,
            messages: [{role: 'assistant', content: 'old transcript'}],
            composer_draft: {text: 'old draft', files: []}, active_stream_id: null
          };
          S.messages = [{role: 'assistant', content: 'old transcript'}];
          document.getElementById('msg').value = 'old draft';
          window.__actionDone = newSession(false, {});
        }
        """
    )
    page.wait_for_function("() => !!window.__pendingRequest('new')")
    page.evaluate(
        """
        () => {
          window.__refreshDone = refreshActiveSessionIfExternallyUpdated('focus')
            .catch(error => {
              window.__refreshError = String(error && error.stack || error);
            })
            .finally(() => { window.__refreshSettled = true; });
        }
        """
    )
    page.wait_for_function(
        """
        () => !!window.__pendingRequest('metadata', 'session-old')
          || !!window.__refreshError
        """
    )
    assert page.evaluate("() => window.__refreshError") is None
    page.evaluate(
        """
        () => window.__resolveRequest('metadata', 'session-old', {session: {
          session_id: 'session-old', profile: 'default', workspace: '/workspace-old',
          message_count: 2, updated_at: 2, last_message_at: 2, messages: []
        }})
        """
    )
    page.evaluate(
        """
        () => window.__resolveRequest('new', null, {session: {
          session_id: 'session-new', profile: 'default', workspace: '/workspace-old', title: 'Untitled',
          message_count: 0, messages: [], composer_draft: {text: '', files: []}, model: 'test-model'
        }})
        """
    )

    page.evaluate("() => window.__actionDone")
    page.wait_for_function(
        """
        () => window.__refreshSettled
          || !!window.__pendingRequest('metadata', 'session-old')
        """
    )
    stale_metadata_pending = page.evaluate(
        "() => !!window.__pendingRequest('metadata', 'session-old')"
    )
    if stale_metadata_pending:
        page.evaluate(
            """
            () => window.__resolveRequest('metadata', 'session-old', {session: {
              session_id: 'session-old', profile: 'default', workspace: '/workspace-old',
              message_count: 2, updated_at: 2, last_message_at: 2, messages: []
            }})
            """
        )
        page.wait_for_function("() => !!window.__pendingRequest('messages', 'session-old')")
        page.evaluate(
            """
            () => window.__resolveRequest('messages', 'session-old', {session: {
              session_id: 'session-old', profile: 'default', workspace: '/workspace-old',
              message_count: 2, messages: [{role: 'assistant', content: 'refreshed old transcript'}],
              tool_calls: [], _messages_truncated: false, _messages_offset: 0
            }})
            """
        )
    page.evaluate("() => window.__refreshDone")

    observed = page.evaluate(
        """
        () => ({
          finalSid: S.session && S.session.session_id,
          text: document.getElementById('msg').value,
          files: S.pendingFiles.map(file => file.name),
          oldMetadataCalls: window.__apiLog.filter(
            call => call.kind === 'metadata' && call.sid === 'session-old'
          ).length,
          oldMessageCalls: window.__apiLog.filter(
            call => call.kind === 'messages' && call.sid === 'session-old'
          ).length,
          newSessionPending: _newSessionInFlight !== null,
        })
        """
    )

    assert observed["finalSid"] == "session-new"
    assert observed["text"] == ""
    assert observed["files"] == []
    assert observed["oldMetadataCalls"] == 1, "the stale refresh must stop after its metadata probe"
    assert observed["oldMessageCalls"] == 0
    assert observed["newSessionPending"] is False
    assert errors == []


def test_older_automatic_refresh_response_yields_to_new_chat(production_page):
    page, errors = production_page
    page.evaluate(
        """
        () => {
          S.session = {
            session_id: 'session-old', profile: 'default', workspace: '/workspace-old', title: 'Old',
            message_count: 1, updated_at: 1, last_message_at: 1,
            messages: [{role: 'assistant', content: 'old transcript'}],
            composer_draft: {text: 'old draft', files: []}, active_stream_id: null
          };
          S.messages = [{role: 'assistant', content: 'old transcript'}];
          document.getElementById('msg').value = 'old draft';
          window.__refreshDone = refreshActiveSessionIfExternallyUpdated('focus')
            .catch(error => {
              window.__refreshError = String(error && error.stack || error);
            })
            .finally(() => { window.__refreshSettled = true; });
        }
        """
    )
    page.wait_for_function("() => !!window.__pendingRequest('metadata', 'session-old')")
    page.evaluate(
        """
        () => window.__resolveRequest('metadata', 'session-old', {session: {
          session_id: 'session-old', profile: 'default', workspace: '/workspace-old',
          message_count: 2, updated_at: 2, last_message_at: 2, messages: []
        }})
        """
    )
    page.wait_for_function("() => !!window.__pendingRequest('metadata', 'session-old')")

    page.evaluate("() => { window.__newDone = newSession(false, {}); }")
    page.wait_for_function("() => !!window.__pendingRequest('new')")
    page.evaluate(
        """
        () => window.__resolveRequest('new', null, {session: {
          session_id: 'session-new', profile: 'default', workspace: '/workspace-old', title: 'Untitled',
          message_count: 0, messages: [], composer_draft: {text: '', files: []}, model: 'test-model'
        }})
        """
    )
    page.evaluate("() => window.__newDone")
    page.evaluate(
        """
        () => window.__resolveRequest('metadata', 'session-old', {session: {
          session_id: 'session-old', profile: 'default', workspace: '/workspace-old',
          message_count: 2, updated_at: 2, last_message_at: 2, messages: []
        }})
        """
    )
    page.wait_for_function(
        """
        () => window.__refreshSettled
          || !!window.__pendingRequest('messages', 'session-old')
        """
    )
    stale_messages_pending = page.evaluate(
        "() => !!window.__pendingRequest('messages', 'session-old')"
    )
    if stale_messages_pending:
        page.evaluate(
            """
            () => window.__resolveRequest('messages', 'session-old', {session: {
              session_id: 'session-old', profile: 'default', workspace: '/workspace-old',
              message_count: 2, messages: [{role: 'assistant', content: 'stale old transcript'}],
              tool_calls: [], _messages_truncated: false, _messages_offset: 0
            }})
            """
        )
    page.evaluate("() => window.__refreshDone")

    observed = page.evaluate(
        """
        () => ({
          finalSid: S.session && S.session.session_id,
          text: document.getElementById('msg').value,
          oldMessageCalls: window.__apiLog.filter(
            call => call.kind === 'messages' && call.sid === 'session-old'
          ).length,
          refreshError: window.__refreshError,
          loadingSessionId: _loadingSessionId,
        })
        """
    )

    assert observed["finalSid"] == "session-new"
    assert observed["text"] == ""
    assert observed["oldMessageCalls"] == 0
    assert observed["refreshError"] is None
    assert observed["loadingSessionId"] is None
    assert errors == []


@pytest.mark.parametrize(("stage", "worktree"), [(stage, worktree) for stage in ("draft", "workspace", "cleanup") for worktree in (False, True)] + [("draft", None)])
@pytest.mark.parametrize("action", ["send", "command", "voice"])
@pytest.mark.parametrize("other_profile", [False, True], ids=["same-profile", "cross-profile"])
def test_late_navigation_supersedes_created_session(
    production_page, stage, action, other_profile, worktree
):
    page, errors = production_page
    page.set_viewport_size({"width": 1280 if action == "send" else 390, "height": 800})
    text = "/local keep-this" if action == "command" else "keep-this"
    page.evaluate(
        r"""({stage, text, worktree}) => {
          __enableWorkspace();
          window.__holdDraft = stage === 'draft';
          window.__holdWorkspace = stage !== 'draft';
          $('msg').value = text;
          window.__sourceFile = new File(['A'], 'A.txt');
          S.pendingFiles = [window.__sourceFile];
          window.__drains = 0;
          const drain = _drainComposerOwnershipTransition;
          window._drainComposerOwnershipTransition = (...args) => {
            ++window.__drains;
            return drain(...args);
          };
          if (worktree === null) window.__sendDone = send().catch(e => { window.__actionError = String(e.stack || e); });
          window.__actionDone = (worktree === null ? newSession() : newSession(false, {awaitWorkspaceLoad: true, worktree}))
            .then(result => { window.__result = result; });
        }""",
        {"stage": stage, "text": text, "worktree": worktree},
    )
    page.wait_for_function("() => !!__pendingRequest('new')")
    # First-Send has no prior owner, so the complete composer transfers to A.
    page.evaluate("() => __resolveRequest('new', null, {session: {session_id: 'session-a', profile: 'default', workspace: '/workspace-a', messages: [], composer_draft: {}, model: 'test-model'}})")
    page.wait_for_function(
        "() => !!__pendingRequest('draft', 'session-a')" if stage == "draft"
        else "() => !!window.__releaseWorkspace"
    )
    assert page.evaluate("() => S.session.session_id") == "session-a"
    if action == "voice":
        boot = (ROOT / "static" / "boot.js").read_text()
        sr = boot.rindex("const SpeechRecognition=window.SpeechRecognition")
        start = boot.rindex("(function(){", 0, sr)
        end = boot.index("\n})();", boot.index("window._voiceModeImmediateSend=_voiceModeSend;")) + len("\n})();")
        page.evaluate(r"""() => {
          document.body.insertAdjacentHTML('beforeend', '<div id="voiceModeBar"></div><div id="voiceModeIndicator"></div><div id="voiceModeLabel"></div>');
          window.SpeechRecognition = class { start() {} abort() {} stop() {} };
          window._locale = {_speech: 'en-US'};
          window._micOriginNeedsSecureContext = () => false;
          window._setButtonTooltip = () => {};
          window._clearBrowserTtsRecovery = () => {};
          window.stopTTS = () => {};
        }""")
        page.add_script_tag(content=boot[start:end])
        page.evaluate("() => { $('btnVoiceMode').onclick(); window._voiceModeImmediateSend(); }")
    elif worktree is not None:
        page.evaluate("() => { window.__sendDone = send().catch(e => { window.__actionError = String(e.stack || e); }); }")
    page.evaluate(
        r"""({otherProfile, stage}) => {
          const profile = otherProfile ? 'other' : 'default';
          _showAllProfiles = true;
          _rememberComposerPendingFiles('session-b', [new File(['B'], 'B.txt')], profile);
          const navigate = () => {
            window.__navigationDone = _openSidebarSession({session_id: 'session-b', profile}, {force: true})
              .catch(e => { window.__navigationError = String(e.stack || e); });
          };
          if (stage === 'cleanup') {
            const setPending = _setNewSessionPending;
            window._setNewSessionPending = pending => {
              setPending(pending);
              if (!pending) navigate();
            };
          } else navigate();
        }""", {"otherProfile": other_profile, "stage": stage}
    )
    assert page.evaluate("() => !!__pendingRequest('metadata', 'session-b')") is False
    if stage == "draft":
        page.evaluate("() => { window.__holdDraft = false; __resolveRequest('draft', 'session-a', {}); }")
    else:
        page.evaluate("() => window.__releaseWorkspace()")
    page.wait_for_function("() => !!__pendingRequest('metadata', 'session-b') || !!window.__navigationError")
    assert page.evaluate("() => window.__navigationError") is None
    profile = "other" if other_profile else "default"
    page.evaluate(
        "profile => __resolveRequest('metadata', 'session-b', {session: {session_id: 'session-b', profile, workspace: '/workspace-b', messages: [], message_count: 1, composer_draft: {text: 'draft B', files: []}}})", profile
    )
    page.wait_for_function("() => !!__pendingRequest('messages', 'session-b')")
    page.evaluate("profile => __resolveRequest('messages', 'session-b', {session: {session_id: 'session-b', profile, workspace: '/workspace-b', messages: [{role: 'assistant', content: 'B transcript'}], message_count: 1, tool_calls: []}})", profile)
    page.evaluate("() => Promise.all([window.__actionDone, window.__navigationDone, window.__sendDone])")
    page.evaluate("() => Promise.resolve().then(() => Promise.resolve())")
    observed = page.evaluate(r"""() => ({
      result: window.__result, sid: S.session.session_id, profile: S.activeProfile,
      workspace: S.session.workspace, entries: S.entries, text: $('msg').value,
      files: S.pendingFiles.map(f => f.name), chatStarts: __apiLog.filter(c => c.kind === 'chat-start'),
      commands: __commandCalls, drains: __drains, error: __actionError, toasts: __toasts,
      draftWrites: __apiLog.filter(c => c.kind === 'draft' && c.sid === 'session-a'),
      remembered: __rememberedNewChatDraft,
      snapshot: _composerRememberedOwnerSnapshot('session-a', 'default'),
      liveFileNames: (_composerPendingFilesByOwner.get(_composerPendingFilesOwnerKey('session-a', 'default')) || []).map(f => f.name),
      createBody: __apiLog.find(c => c.kind === 'new').body,
      sameLiveFile: _composerRememberedOwnerSnapshot('session-a', 'default').files[0] === window.__sourceFile,
    })""")
    assert observed["result"]["status"] == "superseded", observed
    assert observed["result"]["session"]["session_id"] == "session-a"
    assert observed["chatStarts"] == []
    assert observed["commands"] == []
    assert observed["error"] is None
    assert observed["drains"] == 1, "the destination transition must only drain once"
    assert observed["snapshot"]["text"] == text
    assert observed["liveFileNames"] == ["A.txt"]
    assert observed["sameLiveFile"] is True
    assert observed["draftWrites"][-1]["body"]["text"] == text
    assert [file["name"] for file in observed["draftWrites"][-1]["body"]["files"]] == ["A.txt"]
    if worktree is None:
        assert "worktree" not in observed["createBody"]
    else:
        assert observed["createBody"]["worktree"] is worktree
    assert (observed["sid"], observed["profile"], observed["workspace"]) == ("session-b", profile, "/workspace-b")
    assert observed["text"] == "draft B"
    assert observed["files"] == ["B.txt"]
    assert observed["entries"] == [{"name": "B.txt"}]
    if not worktree:
        assert observed["remembered"]["session_id"] == "session-a"
        assert observed["remembered"]["composer_draft"]["text"] == text
    else:
        assert observed["remembered"] is None
    assert errors == []


@pytest.mark.parametrize("stage", ["draft", "workspace"])
@pytest.mark.parametrize("action", ["send", "command"])
def test_direct_profile_choice_supersedes_pending_new_chat(production_page, stage, action):
    page, errors = production_page
    text = "/local keep-this" if action == "command" else "keep-this"
    page.evaluate(r"""({stage, text}) => {
      __enableWorkspace();
      window.__holdDraft = stage === 'draft';
      window.__holdWorkspace = stage === 'workspace';
      $('msg').value = text;
      S.pendingFiles = [new File(['A'], 'A.txt')];
      window.__actionDone = newSession(false, {awaitWorkspaceLoad: true})
        .then(result => { window.__result = result; });
    }""", {"stage": stage, "text": text})
    page.wait_for_function("() => !!__pendingRequest('new')")
    page.evaluate("() => __resolveRequest('new', null, {session: {session_id: 'session-a', profile: 'default', workspace: '/workspace-a', messages: [], composer_draft: {}}})")
    page.wait_for_function("() => !!__pendingRequest('draft', 'session-a')" if stage == "draft" else "() => !!window.__releaseWorkspace")
    page.evaluate("() => { window.__sendDone = send(); window.__navigationDone = switchToProfile('other'); }")
    if stage == "draft":
        page.evaluate("() => { window.__holdDraft = false; __resolveRequest('draft', 'session-a', {}); }")
    else:
        page.evaluate("() => window.__releaseWorkspace()")
    page.wait_for_function("() => !!__pendingRequest('new')")
    page.evaluate("() => __resolveRequest('new', null, {session: {session_id: 'session-b', profile: 'other', workspace: '/workspace-b', messages: [], composer_draft: {}}})")
    page.evaluate("() => Promise.all([window.__actionDone, window.__sendDone, window.__navigationDone])")
    observed = page.evaluate("() => ({result: __result, sid: S.session.session_id, profile: S.activeProfile, text: $('msg').value, files: S.pendingFiles.map(f => f.name), commands: __commandCalls, starts: __apiLog.filter(c => c.kind === 'chat-start'), snapshot: _composerRememberedOwnerSnapshot('session-a', 'default')})")
    assert observed["result"]["status"] == "superseded"
    assert observed["sid"] == "session-b"
    assert observed["profile"] == "other"
    assert observed["text"] == ""
    assert observed["files"] == []
    assert observed["commands"] == []
    assert observed["starts"] == []
    assert observed["snapshot"]["text"] == text
    assert errors == []


@pytest.mark.parametrize('stage', ['draft', 'workspace'])
@pytest.mark.parametrize('action', ['send', 'command'])
@pytest.mark.parametrize('other_profile', [False, True], ids=['same-profile-C', 'cross-profile-C'])
def test_newer_sidebar_choice_retires_queued_direct_profile_intent(
    production_page, stage, action, other_profile
):
    """Exact review schedule: A settling, direct profile B queued, then sidebar C."""
    page, errors = production_page
    text = '/local keep-this' if action == 'command' else 'keep-this'
    page.evaluate(r'''({stage, text}) => {
      __enableWorkspace();
      window.__holdDraft = stage === 'draft';
      window.__holdWorkspace = stage === 'workspace';
      $('msg').value = text;
      S.pendingFiles = [new File(['A'], 'A.txt')];
      window.__actionDone = newSession(false, {awaitWorkspaceLoad: true})
        .then(result => { window.__result = result; });
    }''', {'stage': stage, 'text': text})
    page.wait_for_function("() => !!__pendingRequest('new')")
    page.evaluate("() => __resolveRequest('new', null, {session: {session_id: 'session-a', profile: 'default', workspace: '/workspace-a', messages: [], composer_draft: {}}})")
    page.wait_for_function("() => !!__pendingRequest('draft', 'session-a')" if stage == 'draft' else "() => !!window.__releaseWorkspace")
    profile = 'third' if other_profile else 'default'
    page.evaluate(r'''profile => {
      window.__sendDone = send();
      window.__profileDone = switchToProfile('other').then(result => { window.__profileResult = result; });
      _showAllProfiles = true;
      _rememberComposerPendingFiles('session-c', [new File(['C'], 'C.txt')], profile);
      window.__navigationDone = _openSidebarSession({session_id: 'session-c', profile}, {force: true});
    }''', profile)
    assert page.evaluate("() => __apiLog.filter(c => c.path === '/api/profile/switch').length") == 0
    if stage == 'draft':
        page.evaluate("() => { window.__holdDraft = false; __resolveRequest('draft', 'session-a', {}); }")
    else:
        page.evaluate("() => window.__releaseWorkspace()")
    page.wait_for_function("() => !!__pendingRequest('metadata', 'session-c') || __apiLog.filter(c => c.kind === 'new').length > 1")
    before_c = page.evaluate("() => ({creates: __apiLog.filter(c => c.kind === 'new'), switches: __apiLog.filter(c => c.path === '/api/profile/switch'), metadata: !!__pendingRequest('metadata', 'session-c')})")
    assert len(before_c['creates']) == 1, 'retired profile B must not create a replacement session'
    assert [c['body']['name'] for c in before_c['switches']] == (['third'] if other_profile else [])
    assert before_c['metadata'], before_c
    page.evaluate("profile => __resolveRequest('metadata', 'session-c', {session: {session_id: 'session-c', profile, workspace: '/workspace-c', messages: [], message_count: 1, composer_draft: {text: 'draft C', files: []}}})", profile)
    page.wait_for_function("() => !!__pendingRequest('messages', 'session-c')")
    page.evaluate("profile => __resolveRequest('messages', 'session-c', {session: {session_id: 'session-c', profile, workspace: '/workspace-c', messages: [{role: 'assistant', content: 'C transcript'}], message_count: 1, tool_calls: []}})", profile)
    page.evaluate("() => Promise.all([__actionDone, __profileDone, __navigationDone, __sendDone, _waitForContextTransitionSettlement()])")
    observed = page.evaluate(r'''() => ({
      result: __result, profileResult: __profileResult, sid: S.session.session_id,
      profile: S.activeProfile, text: $('msg').value, files: S.pendingFiles.map(f => f.name),
      messages: S.messages, starts: __apiLog.filter(c => c.kind === 'chat-start'), commands: __commandCalls,
      pending: _newSessionRequest !== null || _newSessionInFlight !== null,
      sendPending: _sendInProgress, snapshot: _composerRememberedOwnerSnapshot('session-a', 'default'),
    })''')
    assert observed['result']['status'] == 'superseded'
    assert observed['profileResult'] is False
    assert (observed['sid'], observed['profile'], observed['text'], observed['files']) == ('session-c', profile, 'draft C', ['C.txt'])
    assert observed['messages'] == [{'role': 'assistant', 'content': 'C transcript'}]
    assert observed['snapshot']['text'] == text
    assert observed['commands'] == [] and observed['starts'] == []
    assert observed['pending'] is False and observed['sendPending'] is False
    assert errors == []


@pytest.mark.parametrize('stage', ['profile', 'create', 'draft', 'workspace'])
def test_direct_profile_intent_revalidates_awaits_and_reuses_its_pane_claim(production_page, stage):
    page, errors = production_page
    page.evaluate(r'''stage => {
      __enableWorkspace();
      S.session = {session_id: 'old', profile: 'default', workspace: '/old', messages: [{role: 'user', content: 'old'}]};
      S.messages = S.session.messages;
      $('msg').value = 'old draft';
      if (stage === 'workspace') _workspacePanelMode = 'files';
      window.__holdProfile = stage === 'profile';
      window.__holdWorkspace = stage === 'workspace';
      window.__profileDone = switchToProfile('other').then(result => { window.__profileResult = result; });
      window.__profileClaim = _paneNavigationGeneration;
    }''', stage)
    if stage == 'profile':
        page.wait_for_function("() => !!__pendingRequest('profile')")
    else:
        page.wait_for_function("() => !!__pendingRequest('new')")
        assert page.evaluate("() => _paneNavigationGeneration === __profileClaim"), 'nested New Chat must adopt the direct profile claim'
        if stage in ('draft', 'workspace'):
            page.evaluate("stage => { window.__holdDraft = stage === 'draft'; __resolveRequest('new', null, {session: {session_id: 'session-a', profile: 'other', workspace: '/workspace-a', messages: [], composer_draft: {text: 'destination draft', files: []}}}); }", stage)
            page.wait_for_function("() => !!__pendingRequest('draft', 'session-a')" if stage == 'draft' else "() => !!window.__releaseWorkspace")
    page.evaluate(r'''stage => {
      if (stage !== 'profile') window.__sendDone = send();
      _showAllProfiles = true;
      window.__navigationDone = _openSidebarSession({session_id: 'session-c', profile: 'default'}, {force: true});
      if (stage === 'profile') __resolveRequest('profile', null, {active: 'other', is_default: false});
      else if (stage === 'create') __resolveRequest('new', null, {session: {session_id: 'session-a', profile: 'other', workspace: '/workspace-a', messages: [], composer_draft: {}}});
      else if (stage === 'draft') { window.__holdDraft = false; __resolveRequest('draft', 'session-a', {}); }
      else window.__releaseWorkspace();
    }''', stage)
    page.wait_for_function("() => !!__pendingRequest('metadata', 'session-c')")
    page.evaluate("() => __resolveRequest('metadata', 'session-c', {session: {session_id: 'session-c', profile: 'default', workspace: '/workspace-c', messages: [], message_count: 1, composer_draft: {text: 'draft C', files: []}}})")
    page.wait_for_function("() => !!__pendingRequest('messages', 'session-c')")
    page.evaluate("() => __resolveRequest('messages', 'session-c', {session: {session_id: 'session-c', profile: 'default', workspace: '/workspace-c', messages: [{role: 'assistant', content: 'C'}], message_count: 1, tool_calls: []}})")
    page.evaluate("() => Promise.all([__profileDone, __navigationDone, window.__sendDone, _waitForContextTransitionSettlement()])")
    observed = page.evaluate(r'''() => ({
      result: __profileResult, sid: S.session.session_id, profile: S.activeProfile,
      creates: __apiLog.filter(c => c.kind === 'new').length,
      starts: __apiLog.filter(c => c.kind === 'chat-start'), commands: __commandCalls,
      pending: _newSessionRequest !== null || _newSessionInFlight !== null || _sendInProgress,
      text: $('msg').value, embargo: _profileSwitchListEmbargo, disabled: $('btnNewChat').disabled,
      toasts: __toasts,
    })''')
    assert observed['result'] is False
    assert (observed['sid'], observed['profile'], observed['text']) == ('session-c', 'default', 'draft C')
    assert observed['creates'] == (0 if stage == 'profile' else 1)
    assert observed['starts'] == [] and observed['commands'] == []
    assert observed['pending'] is False and observed['embargo'] is False and observed['disabled'] is False
    assert not any(toast[0] == 'profile_switched_new_conversation' for toast in observed['toasts'])
    assert errors == []


def test_sidebar_import_cannot_borrow_a_newer_profile_choices_claim(production_page):
    page, errors = production_page
    page.evaluate(r'''() => {
      _showAllProfiles = true;
      _isExternalSession = session => session.session_id === 'older';
      const fixtureApi = api;
      window.api = (path, options) => path === '/api/session/import_cli'
        ? new Promise(resolve => { window.__releaseImport = resolve; })
        : fixtureApi(path, options);
      window.__olderDone = _openSidebarSession({session_id: 'older', profile: 'third'});
    }''')
    page.wait_for_function('() => !!window.__releaseImport')
    page.evaluate("() => { window.__newerDone = switchToProfile('other'); }")
    page.evaluate('() => window.__newerDone')
    page.evaluate('() => { window.__releaseImport({}); }')
    page.evaluate('() => Promise.all([__olderDone, _waitForContextTransitionSettlement()])')
    observed = page.evaluate("() => ({profile: S.activeProfile, switches: __apiLog.filter(c => c.path === '/api/profile/switch').map(c => c.body.name), opens: __apiLog.filter(c => c.kind === 'metadata'), pending: _newSessionRequest !== null})")
    assert observed == {'profile': 'other', 'switches': ['other'], 'opens': [], 'pending': False}
    assert errors == []
