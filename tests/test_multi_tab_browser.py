"""Real Chromium two-tab proof for per-document session storage isolation.

Unlike the Node helper harness, this uses one browser context (shared origin
localStorage), two real browsing contexts (distinct sessionStorage), and a real
navigation/reload of the current WebUI page. No agent turn is started.
"""

import time

import pytest


def test_two_browser_tabs_keep_separate_active_sessions_through_switch_and_reload(base_url):
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        try:
            context = browser.new_context(base_url=base_url)
            tab_a = context.new_page()
            tab_b = context.new_page()
            for tab in (tab_a, tab_b):
                tab.goto("/", wait_until="domcontentloaded")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if tab.evaluate("typeof S !== 'undefined' && S._bootReady === true"):
                        break
                    time.sleep(0.1)
                assert tab.evaluate("S._bootReady === true")

            def state(tab):
                return tab.evaluate("""() => {
                    const id = sessionStorage.getItem('hermes-webui-tab-id');
                    return {
                        id,
                        active: typeof _rememberedActiveSession === 'function'
                            ? _rememberedActiveSession() : localStorage.getItem('hermes-webui-session'),
                        scoped: id ? localStorage.getItem('hermes-webui-session::' + id) : null,
                        mirror: sessionStorage.getItem('hermes-webui-tab-active-session'),
                    };
                }""")

            # Create two real sessions in the server's disposable test state.
            # newSession/loadSession are the same navigation entrypoints as the
            # New Chat and conversation-list actions; no mocked response is used.
            sid_a = tab_a.evaluate("""async () => {
                await newSession(false, {worktree:false});
                return S.session.session_id;
            }""")
            sid_b = tab_b.evaluate("""async () => {
                await newSession(false, {worktree:false});
                return S.session.session_id;
            }""")
            assert sid_a and sid_b and sid_a != sid_b
            first_a, first_b = state(tab_a), state(tab_b)
            assert first_a["id"] and first_b["id"] and first_a["id"] != first_b["id"]
            assert first_a["active"] == first_a["scoped"] == first_a["mirror"] == sid_a
            assert first_b["active"] == first_b["scoped"] == first_b["mirror"] == sid_b

            # Switch active sessions independently through the real loader.
            tab_a.evaluate("async sid => await loadSession(sid)", sid_b)
            tab_b.evaluate("async sid => await loadSession(sid)", sid_a)
            tab_a.evaluate("async sid => await loadSession(sid)", sid_a)
            tab_b.evaluate("async sid => await loadSession(sid)", sid_b)
            assert state(tab_a)["active"] == sid_a
            assert state(tab_b)["active"] == sid_b
            assert tab_a.evaluate("S.session.session_id") == sid_a
            assert tab_b.evaluate("S.session.session_id") == sid_b

            # These are empty, unsent chats, so the test server may not keep a
            # durable row for boot to display. The recovery invariant here is
            # the actual browser-owned session selection, not backend persistence.
            tab_a.reload(wait_until="domcontentloaded")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if tab_a.evaluate("typeof S !== 'undefined' && S._bootReady === true"):
                    break
                time.sleep(0.1)
            assert tab_a.evaluate("S._bootReady === true")
            reloaded_a, unchanged_b = state(tab_a), state(tab_b)
            assert reloaded_a["id"] != first_a["id"], "a new document must mint a fresh identity"
            assert reloaded_a["active"] == reloaded_a["scoped"] == reloaded_a["mirror"] == sid_a
            assert unchanged_b == {
                "id": first_b["id"],
                "active": sid_b,
                "scoped": sid_b,
                "mirror": sid_b,
            }
            assert tab_b.evaluate("S.session.session_id") == sid_b
        finally:
            browser.close()
