"""Browser regressions for the durable slash-command /clear flow."""
from __future__ import annotations

import json
import uuid
from urllib.parse import quote
from urllib.request import urlopen

import pytest

from tests.conftest import TEST_BASE, TEST_WORKSPACE


_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
_VIEWPORT_CASES = [
    pytest.param({"width": 1440, "height": 900}, id="desktop"),
    pytest.param({"width": 768, "height": 900}, id="narrow"),
    pytest.param({"width": 390, "height": 844}, id="mobile"),
]


def _seed_session(session_id: str, text: str, *, pinned: bool = False) -> None:
    """Persist a transcript before the real WebUI server reads this session."""
    from api.models import Session

    messages = [
        {"id": f"{session_id}-u", "role": "user", "content": text, "timestamp": 1.0},
        {"id": f"{session_id}-a", "role": "assistant", "content": f"Reply to {text}", "timestamp": 2.0},
    ]
    session = Session(
        session_id=session_id,
        title=f"Clear test {session_id}",
        workspace=str(TEST_WORKSPACE),
        messages=messages,
        context_messages=list(messages),
        pinned=pinned,
    )
    session.save()


def _server_session(session_id: str) -> dict:
    with urlopen(
        f"{TEST_BASE}/api/session?session_id={quote(session_id)}", timeout=10
    ) as response:
        return json.loads(response.read())["session"]


def _browser_or_skip():
    playwright_api = pytest.importorskip("playwright.sync_api")
    return playwright_api


def _open_session(page, session_id: str) -> None:
    page.goto(f"{TEST_BASE}/session/{quote(session_id)}", wait_until="domcontentloaded")
    page.wait_for_function(
        """sid => typeof S !== 'undefined' && S._bootReady === true &&
        S.session && S.session.session_id === sid && Array.isArray(S.messages) && S.messages.length === 2""",
        arg=session_id,
        timeout=15_000,
    )


@pytest.mark.parametrize("pinned", [False, True], ids=["unpinned", "pinned"])
@pytest.mark.parametrize("viewport", _VIEWPORT_CASES)
def test_slash_clear_persists_empty_session_after_reload(
    cleanup_test_sessions, pinned: bool, viewport: dict[str, int]
):
    """A cleared session keeps its identity after reload whether pinned or not."""
    session_id = f"clear_browser_{'pinned' if pinned else 'unpinned'}_{uuid.uuid4().hex}"
    cleanup_test_sessions.append(session_id)
    _seed_session(session_id, "history that must not return", pinned=pinned)

    pw = _browser_or_skip()
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            page = browser.new_page(viewport=viewport)
            _open_session(page, session_id)
            assert "history that must not return" in page.locator("#msgInner").inner_text()

            # Exercise the production slash dispatcher, command implementation,
            # and test-server API rather than a copied/mocked cmdClear function.
            with page.expect_response(
                lambda response: response.url.endswith("/api/session/clear")
                and response.request.method == "POST"
            ) as clear_response:
                page.evaluate("executeCommand('/clear')")
            assert clear_response.value.ok
            page.wait_for_function(
                """sid => S.session && S.session.session_id === sid &&
                Array.isArray(S.messages) && S.messages.length === 0""",
                arg=session_id,
                timeout=10_000,
            )
            clear_payload = clear_response.value.json()
            assert clear_payload["ok"] is True
            assert clear_payload["session"]["session_id"] == session_id
            assert clear_payload["session"]["message_count"] == 0
            assert page.locator("#msgInner").inner_text().strip() == ""

            # The regression: local-only clearing looked correct until a reload
            # rehydrated the transcript from durable session storage.
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(
                """sid => typeof S !== 'undefined' && S._bootReady === true &&
                S.session && S.session.session_id === sid &&
                Array.isArray(S.messages) && S.messages.length === 0""",
                arg=session_id,
                timeout=15_000,
            )
            assert page.locator("#msgInner").inner_text().strip() == ""
        finally:
            browser.close()

    persisted = _server_session(session_id)
    assert persisted["session_id"] == session_id
    assert persisted["messages"] == []
    assert persisted["pinned"] is pinned


def test_slash_clear_api_failure_keeps_visible_and_durable_history(cleanup_test_sessions):
    """A failed clear must not make the transcript disappear only locally."""
    session_id = f"clear_browser_failure_{uuid.uuid4().hex}"
    original_text = "history must remain after failed clear"
    cleanup_test_sessions.append(session_id)
    _seed_session(session_id, original_text)

    pw = _browser_or_skip()
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            page = browser.new_page()
            _open_session(page, session_id)
            page.evaluate(
                """() => {
                    const realApi = window.api.bind(window);
                    window.api = (path, options) => path === '/api/session/clear'
                      ? Promise.reject(new Error('test clear failure'))
                      : realApi(path, options);
                    executeCommand('/clear');
                }"""
            )
            page.wait_for_function(
                """text => document.getElementById('toast').dataset.toastMessage
                .includes(text)""",
                arg="test clear failure",
                timeout=10_000,
            )
            assert original_text in page.locator("#msgInner").inner_text()
            assert page.evaluate("S.session.session_id") == session_id
        finally:
            browser.close()

    persisted = _server_session(session_id)
    assert [message["content"] for message in persisted["messages"]] == [
        original_text,
        f"Reply to {original_text}",
    ]


def test_late_clear_response_cannot_overwrite_newer_active_session(cleanup_test_sessions):
    """A clear for A cannot replace B while B's load is still unresolved."""
    suffix = uuid.uuid4().hex
    session_a = f"clear_race_a_{suffix}"
    session_b = f"clear_race_b_{suffix}"
    cleanup_test_sessions.extend([session_a, session_b])
    _seed_session(session_a, "session A history")
    _seed_session(session_b, "session B must stay active")

    pw = _browser_or_skip()
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            page = browser.new_page()
            _open_session(page, session_a)
            result = page.evaluate(
                """async ({sessionA, sessionB}) => {
                    const realApi = window.api.bind(window);
                    let delaySessionB = true;
                    window.api = (path, options) => {
                      if (path === '/api/session/clear') {
                        return new Promise((resolve, reject) => {
                          window.__releaseDelayedClear = () => realApi(path, options).then(
                            data => { window.__delayedClearPayload = data; resolve(data); },
                            reject,
                          );
                        });
                      }
                      if (delaySessionB && String(path).startsWith('/api/session?') && String(path).includes(encodeURIComponent(sessionB))) {
                        delaySessionB = false;
                        return new Promise((resolve, reject) => {
                          window.__releaseDelayedSessionB = () => realApi(path, options).then(resolve, reject);
                        });
                      }
                      return realApi(path, options);
                    };
                    const clear = cmdClear();
                    await new Promise(resolve => requestAnimationFrame(resolve));
                    if (typeof window.__releaseDelayedClear !== 'function') {
                      throw new Error('clear request was not started');
                    }
                    const loadingB = loadSession(sessionB);
                    await new Promise(resolve => requestAnimationFrame(resolve));
                    if (typeof window.__releaseDelayedSessionB !== 'function') {
                      throw new Error('session B load was not started');
                    }
                    await window.__releaseDelayedClear();
                    // The clear response is now available while B is still loading.
                    // Release B before awaiting cmdClear's sidebar refresh, which
                    // may itself await the in-flight session-list refresh.
                    await window.__releaseDelayedSessionB();
                    await clear;
                    await loadingB;
                    return {
                      activeSessionId: S.session && S.session.session_id,
                      visibleText: document.getElementById('msgInner').innerText,
                      activeMessages: S.messages.map(message => message.content),
                      clearPayload: window.__delayedClearPayload,
                    };
                }""",
                {"sessionA": session_a, "sessionB": session_b},
            )
        finally:
            browser.close()

    assert result["activeSessionId"] == session_b
    assert result["activeMessages"] == [
        "session B must stay active",
        "Reply to session B must stay active",
    ]
    assert "session B must stay active" in result["visibleText"]
    assert result["clearPayload"]["ok"] is True
    assert result["clearPayload"]["session"]["session_id"] == session_a
    assert result["clearPayload"]["session"]["message_count"] == 0


def test_slash_clear_holds_send_lock_until_durable_clear_finishes(cleanup_test_sessions):
    """A follow-up send cannot overlap the in-flight durable clear request."""
    session_id = f"clear_send_lock_{uuid.uuid4().hex}"
    cleanup_test_sessions.append(session_id)
    _seed_session(session_id, "history cleared before a follow-up")

    pw = _browser_or_skip()
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            page = browser.new_page()
            _open_session(page, session_id)
            result = page.evaluate(
                """async () => {
                    const realApi = window.api.bind(window);
                    window.api = (path, options) => {
                      if (path !== '/api/session/clear') return realApi(path, options);
                      return new Promise((resolve, reject) => {
                        window.__releaseDelayedClear = () => realApi(path, options).then(resolve, reject);
                      });
                    };
                    document.getElementById('msg').value = '/clear';
                    const clearing = send();
                    await new Promise(resolve => requestAnimationFrame(resolve));
                    // The lock itself is the regression boundary. Do not invoke
                    // a synthetic second send here: a real follow-up is queued by
                    // send() while the clear is pending and would turn this into a
                    // test of queueing rather than of /clear's completion lock.
                    const inputWasCleared = document.getElementById('msg').value === '';
                    const lockHeld = _sendInProgress === true;
                    await window.__releaseDelayedClear();
                    await clearing;
                    return {inputWasCleared, lockHeld, lockReleased: _sendInProgress === false};
                }"""
            )
        finally:
            browser.close()

    assert result == {"inputWasCleared": True, "lockHeld": True, "lockReleased": True}
    assert _server_session(session_id)["messages"] == []
