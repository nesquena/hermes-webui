"""Production-composed regressions for New Chat navigation supersession."""

from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
MESSAGES_JS = ROOT / "static" / "messages.js"
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
    if (kind === 'draft') return Promise.resolve({});
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
        page.set_content("<!doctype html><html><body></body></html>")
        page.evaluate(_BOOTSTRAP)
        page.add_script_tag(content=COMPOSER_AUTHORITY_JS)
        page.add_script_tag(path=str(SESSIONS_JS))
        page.add_script_tag(path=str(MESSAGES_JS))
        page.evaluate(_OVERRIDES)
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
