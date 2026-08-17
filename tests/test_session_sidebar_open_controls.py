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

    title_append = render.index("titleGroup.appendChild(title);")
    tag_append = render.index("titleGroup.appendChild(chip);")
    assert title_append < tag_append
    assert "title.appendChild(chip);" not in render


def test_title_and_tag_controls_share_a_constrained_layout_group():
    render = _function_source("renderSessionListFromCache")

    group_create = render.index("const titleGroup=document.createElement('div');")
    group_class = render.index("titleGroup.className='session-title-group';")
    title_append = render.index("titleGroup.appendChild(title);")
    tag_block_start = render.index("// Keep tag/filter controls outside")
    tag_block_end = render.index("// Project color dot:", tag_block_start)
    tag_block = render[tag_block_start:tag_block_end]
    tag_append = render.index("titleGroup.appendChild(chip);", tag_block_start, tag_block_end)
    row_append = render.index("titleRow.appendChild(titleGroup);")

    assert group_create < group_class < title_append < tag_append < row_append
    assert "titleRow.appendChild(chip);" not in tag_block


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


def test_tagged_titles_and_focus_ring_fit_narrow_sidebar_in_browser():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the sidebar layout browser test")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 420, "height": 240})
        page.set_content(
            """
            <!doctype html>
            <html class="dark">
              <body>
                <div class="probe">
                  <div class="session-item active" data-sid="session-a">
                    <div class="session-text">
                      <div class="session-title-row">
                        <div class="session-title-group">
                          <button type="button" class="session-title session-open-control" data-sid="session-a">
                            Visible conversation title
                          </button>
                          <span class="session-tag">#alpha</span>
                          <span class="session-tag">#hyphenated-long-tag</span>
                          <span class="session-tag">#gamma</span>
                          <span class="session-tag">#delta</span>
                        </div>
                        <span class="session-time">now</span>
                      </div>
                    </div>
                  </div>
                </div>
              </body>
            </html>
            """
        )
        page.add_style_tag(path=str(ROOT / "static" / "style.css"))
        page.add_style_tag(content="body{margin:0}.probe{margin:8px}")

        page.keyboard.press("Tab")
        control = page.locator(".session-open-control")
        assert control.evaluate("el => el.matches(':focus-visible')") is True
        page.wait_for_timeout(200)

        metrics = []
        for width in (260, 300):
            page.locator(".probe").evaluate("(el, width) => { el.style.width = width + 'px'; }", width)
            metrics.append(
                page.locator(".session-item").evaluate(
                    """
                    row => {
                      const titleRow = row.querySelector('.session-title-row');
                      const group = row.querySelector('.session-title-group');
                      const title = row.querySelector('.session-open-control');
                      const tags = Array.from(row.querySelectorAll('.session-tag'));
                      const rowStyle = getComputedStyle(row);
                      const titleStyle = getComputedStyle(title);
                      const groupRect = group.getBoundingClientRect();
                      const rowRect = titleRow.getBoundingClientRect();
                      return {
                        titleWidth: title.getBoundingClientRect().width,
                        rowClientWidth: titleRow.clientWidth,
                        rowScrollWidth: titleRow.scrollWidth,
                        groupInsideRow:
                          groupRect.left >= rowRect.left - 1 &&
                          groupRect.right <= rowRect.right + 1,
                        tagsSingleLine: tags.every(tag => {
                          const style = getComputedStyle(tag);
                          return style.whiteSpace === 'nowrap' &&
                            style.overflowX === 'hidden' &&
                            style.textOverflow === 'ellipsis';
                        }),
                        tagsFitRowHeight: tags.every(tag =>
                          tag.getBoundingClientRect().height <= rowRect.height + 1
                        ),
                        rowBoxShadow: rowStyle.boxShadow,
                        buttonOutlineStyle: titleStyle.outlineStyle,
                      };
                    }
                    """
                )
            )
        browser.close()

    for result in metrics:
        assert result["titleWidth"] >= 36
        assert result["rowScrollWidth"] <= result["rowClientWidth"] + 1
        assert result["groupInsideRow"] is True
        assert result["tagsSingleLine"] is True
        assert result["tagsFitRowHeight"] is True
        assert "inset" in result["rowBoxShadow"]
        assert re.search(r"\b2px\b", result["rowBoxShadow"])
        assert result["buttonOutlineStyle"] == "none"


def test_tagged_inline_rename_preserves_input_width_in_narrow_sidebar():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the sidebar rename layout browser test")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1024, "height": 240})
        page.set_content(
            """
            <!doctype html>
            <html class="dark">
              <body>
                <div class="probe">
                  <div class="session-item active" data-sid="session-a">
                    <div class="session-text">
                      <div class="session-title-row">
                        <div class="session-title-group">
                          <input class="session-title-input" value="Visible conversation title">
                          <span class="session-tag">#alpha</span>
                          <span class="session-tag">#hyphenated-long-tag</span>
                          <span class="session-tag">#gamma</span>
                          <span class="session-tag">#delta</span>
                        </div>
                        <span class="session-time">now</span>
                      </div>
                    </div>
                    <div class="session-actions"></div>
                  </div>
                </div>
              </body>
            </html>
            """
        )
        page.add_style_tag(path=str(ROOT / "static" / "style.css"))
        page.add_style_tag(content="body{margin:0}.probe{margin:8px}")

        rename_input = page.locator(".session-title-input")
        rename_input.focus()
        page.wait_for_timeout(100)

        metrics = []
        for viewport_width, sidebar_width in ((1024, 180), (420, 280)):
            page.set_viewport_size({"width": viewport_width, "height": 240})
            page.locator(".probe").evaluate(
                "(el, width) => { el.style.width = width + 'px'; }", sidebar_width
            )
            metrics.append(
                page.locator(".session-item").evaluate(
                    """
                    row => {
                      const titleRow = row.querySelector('.session-title-row');
                      const input = row.querySelector('.session-title-input');
                      const tags = Array.from(row.querySelectorAll('.session-tag'));
                      return {
                        inputWidth: input.getBoundingClientRect().width,
                        rowClientWidth: titleRow.clientWidth,
                        rowScrollWidth: titleRow.scrollWidth,
                        tagsHidden: tags.every(tag => getComputedStyle(tag).display === 'none'),
                      };
                    }
                    """
                )
            )
        browser.close()

    for result in metrics:
        assert result["tagsHidden"] is True
        assert result["inputWidth"] >= 80
        assert result["rowScrollWidth"] <= result["rowClientWidth"] + 1
