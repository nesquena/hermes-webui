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
            # Explicit URL routing independently selects the same durable chat;
            # this is not authority from inherited sessionStorage or a release
            # marker. The about:blank copied-tab test below has no such route.
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


def test_copied_browser_tab_waits_until_original_closes_before_initializing(base_url):
    """Real Chromium copies sessionStorage at popup creation; WebUI JS runs later."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        try:
            context = browser.new_context(base_url=base_url)
            original = context.new_page()
            original.goto("/", wait_until="domcontentloaded")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if original.evaluate("typeof S !== 'undefined' && S._bootReady === true"):
                    break
                time.sleep(0.1)
            assert original.evaluate("S._bootReady === true")
            sid = original.evaluate("""async () => {
                await newSession(false, {worktree:false});
                return S.session.session_id;
            }""")
            predecessor = original.evaluate("sessionStorage.getItem('hermes-webui-tab-id')")
            assert original.evaluate(
                "id => localStorage.getItem('hermes-webui-session::'+id)", predecessor
            ) == sid
            original.evaluate("""({id, sid}) => {
                const marker = JSON.stringify({sid, streamId:'stream-test', ts:Date.now()});
                const snapshot = JSON.stringify({[sid]:{
                    streamId:'stream-test', updated_at:Date.now(), tabId:id,
                    messages:[{role:'assistant', content:'private in-flight text'}],
                }});
                localStorage.setItem('hermes-webui-inflight::'+id, marker);
                localStorage.setItem('hermes-webui-inflight-state::'+id, snapshot);
                sessionStorage.setItem('hermes-webui-tab-inflight', marker);
                sessionStorage.setItem('hermes-webui-tab-inflight-state', snapshot);
            }""", {"id": predecessor, "sid": sid})

            # about:blank inherits the origin and snapshots its sessionStorage.
            # It does not load ui.js until after the original has closed.
            with original.expect_popup() as popup_info:
                original.evaluate("window.open('about:blank', '_blank')")
            duplicate = popup_info.value
            assert duplicate.evaluate("sessionStorage.getItem('hermes-webui-tab-id')") == predecessor
            original.close()
            assert duplicate.evaluate("""id => {
                const marker = localStorage.getItem('hermes-webui-tab-released::'+id);
                return marker !== null && Number.isFinite(Number(marker));
            }""", predecessor), "the predecessor must be released before the duplicate runs"
            duplicate.goto("/", wait_until="domcontentloaded")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if duplicate.evaluate("typeof S !== 'undefined' && S._bootReady === true"):
                    break
                time.sleep(0.1)
            assert duplicate.evaluate("S._bootReady === true")
            state = duplicate.evaluate("""() => {
                const id = sessionStorage.getItem('hermes-webui-tab-id');
                return {
                    id,
                    active: _rememberedActiveSession(),
                    scoped: localStorage.getItem('hermes-webui-session::'+id),
                    mirror: sessionStorage.getItem('hermes-webui-tab-active-session'),
                    inflight: localStorage.getItem('hermes-webui-inflight::'+id),
                    transcript: localStorage.getItem('hermes-webui-inflight-state::'+id),
                    copiedTranscript: sessionStorage.getItem('hermes-webui-tab-inflight-state'),
                };
            }""")
            assert state["id"] != predecessor
            assert state["active"] is state["scoped"] is state["mirror"] is None
            assert state["inflight"] is state["transcript"] is state["copiedTranscript"] is None
            assert duplicate.evaluate(
                "id => localStorage.getItem('hermes-webui-session::'+id)", predecessor
            ) == sid
            assert duplicate.evaluate(
                "id => localStorage.getItem('hermes-webui-inflight-state::'+id)", predecessor
            ) is not None
        finally:
            browser.close()
