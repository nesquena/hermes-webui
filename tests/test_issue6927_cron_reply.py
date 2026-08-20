"""Focused served-composer coverage for issue #6927."""

from pathlib import Path

import pytest

from tests.conftest import TEST_BASE


def _page():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright is not installed")
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page()
    page.goto(TEST_BASE + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#msg")
    return pw, browser, page


def _close(pw, browser):
    browser.close()
    pw.stop()


def test_issue6927_served_composer_automatically_continues_canonical_cron_reply():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls = [];
          const noop = () => {};
          ['renderSessionList','renderSessionListFromCache','renderMessages','renderTray',
           'startApprovalPolling','startClarifyPolling','_fetchYoloState','ensureLiveWorklogShell',
           'clearLiveToolCards','updateSendBtn','autoResize','hideCmdDropdown','removeThinking',
           'clearOptimisticSessionStreaming','clearInflightState','saveInflightState',
           'upsertActiveSessionForLocalTurn','applySessionTitleUpdate'].forEach(k => window[k] = noop);
          window.setBusy = value => { S.busy = !!value; };
          window.uploadPendingFiles = async () => [];
          window.attachLiveStream = noop;
          S.session = {session_id:'cron-source', raw_source:'cron', read_only:true,
            model:'m', model_provider:'p', profile:'prof', workspace:'/w'};
          S.messages = [{role:'assistant', content:'completed'}];
          $('msg').value = 'continue this';
          window.api = async (url, opts) => {
            calls.push({url, opts});
            if (url === '/api/session/branch') return {session_id:'child-6927'};
            if (url === '/api/session/draft') return {};
            if (url === '/api/chat/start') return {stream_id:'stream-6927'};
            throw new Error('unexpected request ' + url);
          };
          window.loadSession = async sid => {
            _loadSessionGeneration += 1;
            S.session = {session_id:sid, session_source:'fork', read_only:false,
              model:'m', model_provider:'p', profile:'prof', workspace:'/w', composer_draft:{text:'continue this', files:[]}};
            _loadingSessionId = null;
          };
          await send();
          return {urls:calls.map(c => c.url), child:S.session.session_id,
            start:calls.find(c => c.url === '/api/chat/start')?.opts,
            text:$('msg').value};
        }""")
        assert result["urls"] == ["/api/session/branch", "/api/session/draft", "/api/chat/start", "/api/session/draft"]
        assert result["child"] == "child-6927"
        assert result["start"].get("retries") == 0
        assert result["text"] == ""
    finally:
        _close(pw, browser)


def test_non_cron_source_and_id_shape_remain_refused():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls = [];
          S.session = {session_id:'cron-looking-id', raw_source:'messaging', session_source:'cron', read_only:true};
          $('msg').value = 'raw source is non-cron';
          window.api = async url => { calls.push(url); return {}; };
          await send();
          return {calls, text:$('msg').value};
        }""")
        assert result == {"calls": [], "text": "raw source is non-cron"}
    finally:
        _close(pw, browser)


@pytest.mark.parametrize("command", ["/compress", "/retry", "/undo"])
def test_mutating_commands_remain_refused_on_read_only_cron(command):
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async command => {
          const calls = [];
          S.session = {session_id:'cron-command', raw_source:'cron', read_only:true};
          $('msg').value = command;
          window.queueSessionMessage = () => calls.push('queue');
          window.api = async url => { calls.push(url); return {}; };
          await send();
          return {calls, text:$('msg').value};
        }""", command)
        assert result["calls"] == []
        assert result["text"] == command
    finally:
        _close(pw, browser)


def test_child_draft_failure_keeps_complete_payload():
    pw, browser, page = _page()
    try:
        result = page.evaluate("""async () => {
          const calls = [];
          const file = new File(['bytes'], 'live.txt');
          S.session = {session_id:'cron-draft-failure', raw_source:'cron', read_only:true};
          S.pendingFiles = [file]; $('msg').value = 'child draft acknowledgement failed';
          window.api = async (url) => { calls.push(url); if (url === '/api/session/branch') return {session_id:'child-failure'}; throw new Error('child draft acknowledgement failed'); };
          await send();
          return {calls, text:$('msg').value, files:S.pendingFiles.length,
            file:S.pendingFiles[0] ? S.pendingFiles[0].name : null};
        }""")
        assert result["calls"] == ["/api/session/branch", "/api/session/draft"]
        assert result["text"] == "child draft acknowledgement failed"
        assert result["file"] == "live.txt"
    finally:
        _close(pw, browser)


def test_ordinary_writable_send_keeps_default_network_retry():
    source = (Path(__file__).parents[1] / "static" / "messages.js").read_text(encoding="utf-8")
    start = source.index("/api/chat/start")
    assert "retries:0" not in source[start:start + 1200]
