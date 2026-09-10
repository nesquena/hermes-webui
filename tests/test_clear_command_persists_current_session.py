"""Browser regressions for the durable slash-command /clear flow."""
from __future__ import annotations

import json
from urllib.parse import quote
from urllib.request import urlopen

import pytest

from tests.conftest import TEST_BASE, TEST_WORKSPACE


_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def _seed_session(session_id: str, text: str) -> None:
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
        session_id,
        timeout=15_000,
    )


def test_slash_clear_persists_empty_pinned_session_after_reload(cleanup_test_sessions):
    """The real slash command clears server state and keeps the same session ID."""
    session_id = "clear_browser_pinned"
    cleanup_test_sessions.append(session_id)
    _seed_session(session_id, "history that must not return")

    pw = _browser_or_skip()
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            page = browser.new_page()
            _open_session(page, session_id)
            assert "history that must not return" in page.locator("#msgInner").inner_text()

            # Exercise the production slash dispatcher, command implementation,
            # and test-server API rather than a copied/mocked cmdClear function.
            page.evaluate("executeCommand('/clear')")
            page.wait_for_function(
                """sid => S.session && S.session.session_id === sid &&
                Array.isArray(S.messages) && S.messages.length === 0""",
                session_id,
                timeout=10_000,
            )
            assert page.locator("#msgInner").inner_text().strip() == ""

            # The regression: local-only clearing looked correct until a reload
            # rehydrated the transcript from durable session storage.
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(
                """sid => typeof S !== 'undefined' && S._bootReady === true &&
                S.session && S.session.session_id === sid &&
                Array.isArray(S.messages) && S.messages.length === 0""",
                session_id,
                timeout=15_000,
            )
            assert page.locator("#msgInner").inner_text().strip() == ""
        finally:
            browser.close()

    persisted = _server_session(session_id)
    assert persisted["session_id"] == session_id
    assert persisted["messages"] == []
    assert persisted["pinned"] is True


def test_late_clear_response_cannot_overwrite_newer_active_session(cleanup_test_sessions):
    """A clear for session A may finish after the user has opened session B."""
    session_a = "clear_race_a"
    session_b = "clear_race_b"
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
                    window.api = (path, options) => {
                      if (path !== '/api/session/clear') return realApi(path, options);
                      return new Promise((resolve, reject) => {
                        window.__releaseDelayedClear = () => realApi(path, options).then(resolve, reject);
                      });
                    };
                    const clear = cmdClear();
                    await new Promise(resolve => requestAnimationFrame(resolve));
                    if (typeof window.__releaseDelayedClear !== 'function') {
                      throw new Error('clear request was not started');
                    }
                    await loadSession(sessionB);
                    await window.__releaseDelayedClear();
                    await clear;
                    return {
                      activeSessionId: S.session && S.session.session_id,
                      visibleText: document.getElementById('msgInner').innerText,
                      activeMessages: S.messages.map(message => message.content),
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
    assert _server_session(session_a)["messages"] == []
    assert len(_server_session(session_b)["messages"]) == 2
