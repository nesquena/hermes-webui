"""DOM harness for the cron-reply fork handoff."""

import os
import shutil
from pathlib import Path

import pytest

from tests._layout_helpers import assert_layout_sane, assert_no_raw_i18n_keys
from tests.test_465_session_branching import (
    MESSAGES_JS,
    SESSIONS_JS,
    _extract_async_function,
    _extract_function,
)


_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def _page_html() -> str:
    return """\
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <style>
    body { margin: 0; font: 16px sans-serif; background: #111827; color: #f9fafb; }
    .layout { min-height: 100vh; display: flex; flex-direction: column; }
    main { flex: 1; max-width: 960px; margin: 0 auto; padding: 24px; width: 100%; box-sizing: border-box; }
    .session-meta { margin-bottom: 16px; font-size: 14px; opacity: 0.85; }
    #msg { width: 100%; box-sizing: border-box; min-height: 96px; padding: 12px; border-radius: 10px; border: 1px solid #374151; background: #0f172a; color: inherit; }
    .actions { display: flex; gap: 12px; margin-top: 12px; align-items: center; }
    #send { padding: 10px 18px; border: 0; border-radius: 10px; background: #2563eb; color: white; cursor: pointer; }
    #toast { min-height: 24px; color: #bfdbfe; }
  </style>
</head>
<body>
  <div class="layout">
    <main>
      <div class="session-meta">Active session: <span id="activeSession">daily-summary</span></div>
      <textarea id="msg">What changed since yesterday?</textarea>
      <div class="actions">
        <button id="send" type="button">Send</button>
        <div id="toast" aria-live="polite"></div>
      </div>
    </main>
  </div>
</body>
</html>
"""


def test_read_only_cron_send_forks_and_delivers_follow_up(tmp_path):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright is unavailable; run the headed browser proof in CI")

    send_source = _extract_async_function(
        MESSAGES_JS.read_text(encoding="utf-8"), "send"
    )
    is_read_only_source = _extract_function(
        SESSIONS_JS.read_text(encoding="utf-8"), "_isReadOnlySession"
    )
    is_branchable_source = _extract_function(
        SESSIONS_JS.read_text(encoding="utf-8"), "_isBranchableReadOnlySession"
    )
    artifact = tmp_path / "webui5936-after.png"
    export_path = os.environ.get("PR_SWEEP_ARTIFACT_PATH")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.set_content(_page_html())
        page.evaluate(
            """
            ([sendSource, isReadOnlySource, isBranchableSource]) => {
              window.__calls = [];
              window.__toasts = [];
              window.__loadedSessions = [];
              window.__chatStarts = [];
              window.$ = (id) => document.getElementById(id);
              window.t = (key) => ({
                branch_forked: 'Forked into new session',
                branch_failed: 'Fork failed: ',
              }[key] || key);
              window.showToast = (msg) => {
                document.getElementById('toast').textContent = msg;
                window.__toasts.push(msg);
              };
              window.renderTray = () => {};
              window.autoResize = () => {};
              window.renderMessages = () => {};
              window.updateSendBtn = () => {};
              window.renderSessionList = async () => {};
              window._clearComposerDraft = () => Promise.resolve();
              window._restoreComposerDraftAfterFailedSend = (text, files) => {
                document.getElementById('msg').value = text;
                S.pendingFiles = [...files];
              };
              window._flushSelectionBlocksToComposer = () => {};
              window._composerTextWithPendingSelections = () => document.getElementById('msg').value;
              window._pendingSelections = [];
              window._clearPendingSelections = () => {};
              window.shouldInterceptCompressionRecoveryContinuation = () => false;
              window.isCompressionUiRunning = () => false;
              window._clearStaleBusyStateBeforeSend = () => false;
              window._chatPayloadModelState = () => ({
                model: S.session.model || 'default-model',
                model_provider: S.session.model_provider || null,
              });
              window.uploadPendingFiles = async ({ files }) => files.map((file) => ({
                name: file.name,
                path: file.name,
              }));
              window.setBusy = (busy) => { S.busy = busy; };
              window.api = async (url, opts) => {
                const body = JSON.parse(opts.body);
                window.__calls.push({ url, body });
                if (url === '/api/session/branch') {
                  return { session_id: 'forked-session' };
                }
                window.__chatStarts.push(body);
                return { stream_id: 'stream-1' };
              };
              window.loadSession = async (sid) => {
                window.__loadedSessions.push(sid);
                S.session = {
                  session_id: sid,
                  workspace: '/tmp',
                  model: 'child-default',
                  model_provider: 'child-provider',
                  profile: 'child-profile',
                };
                document.getElementById('activeSession').textContent = sid;
              };
              window.ensureLiveWorklogShell = () => {};
              window.clearLiveToolCards = () => {};
              window.appendThinking = () => {};
              window.upsertActiveSessionForLocalTurn = () => {};
              window.renderSessionListFromCache = () => {};
              window.startApprovalPolling = () => {};
              window.startClarifyPolling = () => {};
              window._fetchYoloState = () => {};
              window.applySessionTitleUpdate = () => {};
              window.saveInflightState = () => {};
              window._readPendingSessionModel = () => null;
              window._clearPendingSessionModel = () => {};
              window._forcedSkillDirectivePending = null;
              window._clearComposerAfterQueuedSelectionSend = () => {};
              window._defaultMessageMode = 'queue';
              window.COMMANDS = [];
              window.INFLIGHT = {};
              window._sendInProgress = false;
              window._sendInProgressSid = null;
              window.cancelStream = async () => {};
              window.stopApprovalPolling = () => {};
              window.stopClarifyPolling = () => {};
              window.hideApprovalCard = () => {};
              window.hideClarifyCard = () => {};
              window.removeThinking = () => {};
              window.clearOptimisticSessionStreaming = () => {};
              window.queueSessionMessage = () => {};
              window.updateQueueBadge = () => {};
              window.setComposerStatus = () => {};
              window._isOffline = false;
              window.S = {
                session: {
                  session_id: 'daily-summary',
                  raw_source: 'cron',
                  read_only: true,
                  model: 'chosen-model',
                  model_provider: 'chosen-provider',
                  workspace: '/tmp',
                  profile: 'chosen-profile',
                },
                busy: false,
                pendingFiles: [{ name: 'notes.txt' }],
                messages: [],
                activeProfile: 'chosen-profile',
                toolCalls: [],
              };
              eval(isReadOnlySource);
              eval(isBranchableSource);
              eval(sendSource);
              document.getElementById('send').addEventListener('click', () => send());
            }
            """,
            [send_source, is_read_only_source, is_branchable_source],
        )
        page.locator("#send").click()
        page.wait_for_function(
            """
            () => document.getElementById('activeSession').textContent === 'forked-session'
            """
        )
        assert page.locator("#toast").text_content() == "Forked into new session"
        calls = page.evaluate("window.__calls")
        assert [call["url"] for call in calls] == [
            "/api/session/branch",
            "/api/chat/start",
        ]
        assert calls[1]["body"]["session_id"] == "forked-session"
        assert calls[1]["body"]["message"].startswith("What changed since yesterday?")
        assert "[Attached files: notes.txt]" in calls[1]["body"]["message"]
        assert_layout_sane(
            page,
            scope_selector=".layout > main",
            checks=["overlap", "clip", "container-escape", "raw-string"],
        )
        assert_no_raw_i18n_keys(page, scope_selector=".layout > main")
        page.screenshot(path=str(artifact), full_page=True)
        browser.close()
    if export_path:
        export_artifact = Path(export_path)
        export_artifact.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact, export_artifact)
