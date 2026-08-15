"""Accessibility regressions for sidebar conversation open controls."""

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    marker = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", SESSIONS_JS)
    assert marker, f"{name} not found"
    start = marker.start()
    brace = SESSIONS_JS.find("{", marker.end())
    assert brace >= 0, f"{name} body did not start"
    depth = 1
    index = brace + 1
    while depth and index < len(SESSIONS_JS):
        if SESSIONS_JS[index] == "{":
            depth += 1
        elif SESSIONS_JS[index] == "}":
            depth -= 1
        index += 1
    assert depth == 0, f"{name} body did not close"
    return SESSIONS_JS[start:index]


def test_chat_panel_reuses_visible_label_as_a_heading():
    assert '<h2 class="panel-head-title" data-i18n="tab_chat">Chat</h2>' in INDEX_HTML
    assert ".panel-head-title{" in STYLE_CSS


def test_top_level_conversation_titles_use_separate_native_open_controls():
    render = _function_source("renderSessionListFromCache")

    assert "document.createElement(_sessionSelectMode?'span':'button')" in render
    assert "title.type='button';" in render
    assert "title.className=_sessionSelectMode?'session-title':'session-title session-open-control';" in render
    assert "title.dataset.sid=s.session_id;" in render
    assert "if(isActive) title.setAttribute('aria-current','page');" in render
    assert "_installSessionOpenControl(title,s);" in render

    title_append = render.index("titleRow.appendChild(title);")
    tag_append = render.index("titleRow.appendChild(chip);")
    assert title_append < tag_append
    assert "title.appendChild(chip);" not in render


def test_focus_identity_wraps_the_destructive_list_rebuild():
    render = _function_source("renderSessionListFromCache")
    capture = render.index("const focusedSessionOpenControlId=_captureSessionOpenControlFocus(list);")
    clear = render.index("list.innerHTML='';")
    restore = render.index("_restoreSessionOpenControlFocus(list,focusedSessionOpenControlId);")
    assert capture < clear < restore


def test_select_mode_keeps_titles_out_of_the_tab_order():
    render = _function_source("renderSessionListFromCache")
    create = render.index("document.createElement(_sessionSelectMode?'span':'button')")
    install = render.index("_installSessionOpenControl(title,s);")
    assert create < install
    assert "if(!_sessionSelectMode){" in render[create:install]


def _browser_fixture_script() -> str:
    return "\n".join(
        [
            "let _sessionSelectMode = false;",
            "let _renamingSid = null;",
            "let openCount = 0;",
            "let desktopWidth = true;",
            "let sidebarCollapsed = false;",
            "const $ = id => document.getElementById(id);",
            "const _isDesktopWidth = () => desktopWidth;",
            "const _isSidebarCollapsed = () => sidebarCollapsed;",
            "async function _openSidebarSession(){ openCount += 1; }",
            _function_source("_installSessionOpenControl"),
            _function_source("_isSessionSidebarVisible"),
            _function_source("_captureSessionOpenControlFocus"),
            _function_source("_restoreSessionOpenControlFocus"),
            """
            window.__setupSessionOpenControl = () => {
              document.body.innerHTML = `
                <aside class="sidebar mobile-open">
                  <section id="panelChat" class="active">
                    <input id="sessionSearch" value="">
                    <div id="sessionList">
                      <div class="session-item" data-sid="session-a">
                        <button type="button" class="session-title session-open-control" data-sid="session-a">Alpha</button>
                      </div>
                    </div>
                  </section>
                </aside>`;
              const control = document.querySelector('.session-open-control');
              _installSessionOpenControl(control, {session_id: 'session-a'});
              control.focus();
              return document.activeElement === control;
            };
            window.__sessionOpenCount = () => openCount;
            window.__setSessionSelectMode = value => { _sessionSelectMode = value; };
            window.__focusRoundTrip = keepRow => {
              const list = document.getElementById('sessionList');
              const sid = _captureSessionOpenControlFocus(list);
              list.innerHTML = keepRow
                ? '<div class="session-item" data-sid="session-a"><button type="button" class="session-title session-open-control" data-sid="session-a">Alpha updated</button></div>'
                : '<div class="session-item" data-sid="session-b"><button type="button" class="session-title session-open-control" data-sid="session-b">Beta</button></div>';
              const restored = _restoreSessionOpenControlFocus(list, sid);
              return {
                sid,
                restored,
                activeSid: document.activeElement && document.activeElement.dataset
                  ? document.activeElement.dataset.sid || null
                  : null,
              };
            };
            window.__hiddenMobileFocusRoundTrip = () => {
              const list = document.getElementById('sessionList');
              const sidebar = document.querySelector('.sidebar');
              const sid = _captureSessionOpenControlFocus(list);
              desktopWidth = false;
              sidebar.classList.remove('mobile-open');
              list.innerHTML = '<div class="session-item" data-sid="session-a"><button type="button" class="session-title session-open-control" data-sid="session-a">Alpha hidden</button></div>';
              const restored = _restoreSessionOpenControlFocus(list, sid);
              const result = {
                sid,
                restored,
                activeSid: document.activeElement && document.activeElement.dataset
                  ? document.activeElement.dataset.sid || null
                  : null,
              };
              desktopWidth = true;
              sidebar.classList.add('mobile-open');
              return result;
            };
            """,
        ]
    )


def test_keyboard_activation_and_focus_restore_in_browser():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the sidebar open-control browser test")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        page.set_content("<!doctype html><html><body></body></html>")
        page.add_script_tag(content=_browser_fixture_script())
        assert page.evaluate("window.__setupSessionOpenControl()") is True

        control = page.locator(".session-open-control")
        control.press("Enter")
        control.press("Space")
        keyboard_count = page.evaluate("window.__sessionOpenCount()")

        # A pointer click remains owned by the existing row gesture path. The
        # button's synthesized-click handler must not open the session again.
        control.click()
        pointer_count = page.evaluate("window.__sessionOpenCount()")

        page.evaluate("window.__setSessionSelectMode(true)")
        control.press("Enter")
        select_mode_count = page.evaluate("window.__sessionOpenCount()")

        page.evaluate("window.__setSessionSelectMode(false)")
        page.evaluate("document.querySelector('.session-open-control').focus()")
        kept = page.evaluate("window.__focusRoundTrip(true)")
        page.evaluate("document.querySelector('.session-open-control').focus()")
        hidden = page.evaluate("window.__hiddenMobileFocusRoundTrip()")
        page.evaluate("document.querySelector('.session-open-control').focus()")
        removed = page.evaluate("window.__focusRoundTrip(false)")
        browser.close()

    assert keyboard_count == 2
    assert pointer_count == 2
    assert select_mode_count == 2
    assert kept == {"sid": "session-a", "restored": True, "activeSid": "session-a"}
    assert hidden == {"sid": "session-a", "restored": False, "activeSid": None}
    assert removed == {"sid": "session-a", "restored": False, "activeSid": None}
