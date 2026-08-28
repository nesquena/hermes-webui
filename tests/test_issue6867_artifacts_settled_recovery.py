"""Rendered regression coverage for settled-session Artifacts projection."""

from pathlib import Path
import shutil

import pytest
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static/messages.js").read_text(encoding="utf-8")


def _function_source(source, name):
    marker = f"async function {name}("
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated {name}")


@pytest.fixture(scope="module")
def browser():
    if sync_playwright is None:
        pytest.skip("Playwright is unavailable")
    with sync_playwright() as playwright:
        if not shutil.which("node") or not Path(playwright.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium is unavailable")
        instance = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        yield instance
        instance.close()


def _page(browser):
    page = browser.new_page(viewport={"width": 1024, "height": 600}, device_scale_factor=2)
    page.set_content(
        """
        <main>
          <span id="workspaceArtifactsCount"></span>
          <section id="workspaceArtifacts"></section>
          <button id="workspaceFilesTab"></button>
          <button id="workspaceArtifactsTab"></button>
          <button id="workspaceTodosTab"></button>
          <section id="workspaceFilesPanel"></section>
          <section id="workspaceTodosPanel"></section>
        </main>
        """
    )
    page.add_script_tag(
        content="""
        window.S = {session: null, messages: [], toolCalls: []};
        window.$ = id => document.getElementById(id);
        window.esc = value => String(value).replace(/[&<>\"']/g, c =>
          ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
        window.t = key => key;
        window.switchWorkspacePanelTab = tab => { window.activeTab = tab; };
        window.setStatus = value => { window.lastStatus = value; };
        window.openFile = value => { window.openedPath = value; };
        """
    )
    page.add_script_tag(content=WORKSPACE_JS)
    return page


def test_recovery_replaces_stale_artifact_without_session_switch(browser):
    page = _page(browser)
    try:
        result = page.evaluate(
            """
            async () => {
              window._isSessionCurrentPane = sid =>
                !!sid && !!S.session && S.session.session_id === sid &&
                (!window._loadingSessionId || window._loadingSessionId === sid);
              S.session = {session_id:'session-a', workspace:'/workspace'};
              S.toolCalls = [{name:'write_file', args:{path:'/workspace/old/path.md'}, done:true}];
              renderSessionArtifacts();
              const before = {old:!!document.querySelector('[data-artifact-path="/workspace/old/path.md"]')};
              const recovery = (async () => {
                await Promise.resolve();
                S.messages = [{role:'assistant', content:'settled'}];
                S.toolCalls = [{name:'write_file', args:{path:'/workspace/new/path.md'}, done:true}];
                return projectSessionArtifactsForOwner('session-a');
              })();
              const projected = await recovery;
              return {
                before,
                projected,
                old:!!document.querySelector('[data-artifact-path="/workspace/old/path.md"]'),
                fresh:!!document.querySelector('[data-artifact-path="/workspace/new/path.md"]'),
                count:document.getElementById('workspaceArtifactsCount').textContent,
              };
            }
            """
        )
        assert result == {
            "before": {"old": True},
            "projected": True,
            "old": False,
            "fresh": True,
            "count": "1",
        }
    finally:
        page.close()


def test_foreign_recovery_and_missing_pane_authority_do_not_mutate(browser):
    page = _page(browser)
    try:
        result = page.evaluate(
            """
            () => {
              window._isSessionCurrentPane = sid => sid === 'session-a';
              S.session = {session_id:'session-a', workspace:'/workspace'};
              S.toolCalls = [{name:'write_file', args:{path:'/workspace/keep.md'}, done:true}];
              renderSessionArtifacts();
              const before = document.getElementById('workspaceArtifacts').innerHTML;
              S.toolCalls = [{name:'write_file', args:{path:'/workspace/foreign.md'}, done:true}];
              const foreign = projectSessionArtifactsForOwner('session-b');
              const afterForeign = document.getElementById('workspaceArtifacts').innerHTML;
              delete window._isSessionCurrentPane;
              const unavailable = projectSessionArtifactsForOwner('session-a');
              return {foreign, unavailable, unchanged:before === afterForeign,
                foreignVisible:!!document.querySelector('[data-artifact-path="/workspace/foreign.md"]')};
            }
            """
        )
        assert result == {"foreign": False, "unavailable": False, "unchanged": True, "foreignVisible": False}
    finally:
        page.close()


def test_missing_artifact_path_stays_blocked(browser):
    page = _page(browser)
    try:
        result = page.evaluate(
            """
            async () => {
              S.session = {session_id:'session-a', workspace:'/workspace'};
              window.api = async () => ({entries:[]});
              await openArtifactPath('/workspace/missing.md');
              return {status:window.lastStatus, opened:window.openedPath || null};
            }
            """
        )
        assert result == {"status": "file_open_failed", "opened": None}
    finally:
        page.close()


def test_restore_settled_session_projects_through_production_path(browser):
    page = _page(browser)
    try:
        restore_source = _function_source(MESSAGES_JS, "_restoreSettledSession").replace(
            "catch(_){\n      return returnStatus?'error':false;",
            "catch(error){\n      window.restoreError=String(error);\n      return returnStatus?'error':false;",
        )
        page.add_script_tag(content=restore_source)
        page.evaluate(
            """
            () => {
              window.activeSid = 'session-a';
              window.streamId = 'stream-a';
              S.activeStreamId = 'stream-a';
              window.assistantText = false;
              window.reasoningText = '';
              window._latestGoalStatus = null;
              window._isActiveSession = () => true;
              window._isSessionActivelyViewed = () => false;
              window._clearSource = () => {};
              window._closeSource = () => {};
              window._isSessionCurrentPane = sid => !!S.session && S.session.session_id === sid;
              window._streamFinalized = false;
              window._persistTimer = null;
              window._cancelThrottledSnapshotTimer = () => {};
              window._clearAnchorProseIncrementalNode = () => {};
              window._cancelAnimationFramePendingStreamRender = () => {};
              window._streamFadeCleanupReduceMotionListener = () => {};
              window._smdEndParser = () => {};
              window.finalizeThinkingCard = () => {};
              window._clearOwnerInflightState = () => {};
              window._flushReasoningToAnchor = () => {};
              window._scheduleAnchorRegistryCleanup = () => {};
              window._clearApprovalForOwner = () => {};
              window._clearClarifyForOwner = () => {};
              window.clearLiveToolCards = () => {};
              window.removeThinking = () => {};
              window._markSessionCompletionUnread = () => {};
              window._markSessionViewed = () => {};
              window._messageRenderableMessageCount = () => 1;
              window._currentMessageRenderWindowSize = () => 1;
              window._messageRenderWindowSize = 1;
              window._carryForwardEphemeralTurnFields = (_, next) => next;
              window._filterRecoveryControlMessages = messages => messages;
              window._attachProjectedAnchorSceneToLastAssistant = () => {};
              window._mergeSettledToolCallsWithLiveMetadata = calls => calls;
              window._hydrateTodosFromSession = () => {};
              window._replaceMarkerOnlyAssistantWithStreamError = () => false;
              window.syncTopbar = () => {};
              window.renderMessages = () => {};
              window.renderSessionList = () => {};
              window._setActivePaneIdleIfOwner = () => {};
              window._setActiveSessionUrl = () => {};
              window.localStorage = {setItem: () => {}};
            }
            """
        )
        result = page.evaluate(
            """
            async () => {
              S.session = {session_id:'session-a', workspace:'/workspace'};
              S.toolCalls = [{name:'write_file', args:{path:'/workspace/old.md'}, done:true}];
              renderSessionArtifacts();
              window.api = async () => ({session:{
                session_id:'session-b', workspace:'/workspace', active_stream_id:null,
                pending_user_message:null, messages:[], tool_calls:[
                  {name:'write_file', args:{path:'/workspace/new.md'}, done:true}
                ]
              }});
              const status = await _restoreSettledSession({close:()=>{}}, {status:true});
              return {
                status,
                error: window.restoreError || null,
                old: !!document.querySelector('[data-artifact-path="/workspace/old.md"]'),
                fresh: !!document.querySelector('[data-artifact-path="/workspace/new.md"]')
              };
            }
            """
        )
        assert result == {"status": "restored", "error": None, "old": False, "fresh": True}
    finally:
        page.close()
