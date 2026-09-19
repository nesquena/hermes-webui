"""Static frontend tests for the in-chat todos tray (feat/todos-in-chat).

These verify that the chat-embedded task tray wiring stays intact:
  - the tray DOM exists in index.html next to the message shell
  - the settings checkbox exists and is wired in loadSettingsPanel
  - the scheduler fans out to renderChatTodos on every todo_state refresh
  - the render path escapes user content and marks terminal states
  - the rail-hide helper targets [data-panel="todos"] with nav-tab-hidden
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def _read_static(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_chat_todos_tray_markup_exists_in_message_shell():
    idx = _read_static("static/index.html")
    assert 'id="chatTodosPanel"' in idx
    assert 'id="chatTodosHead"' in idx
    assert 'id="chatTodosSummary"' in idx
    assert 'id="chatTodosCounter"' in idx
    assert 'id="chatTodosBody"' in idx
    # The tray must live inside the messages shell, before the messages node,
    # so it stays pinned at the top of the chat area (not inside the scroller).
    shell = idx.find('class="messages-shell"')
    tray = idx.find('id="chatTodosPanel"')
    messages = idx.find('id="messages"')
    assert shell != -1 and tray != -1 and messages != -1
    assert shell < tray < messages


def test_chat_todos_settings_checkbox_wired_in_settings_panel():
    idx = _read_static("static/index.html")
    panels = _read_static("static/panels.js")

    assert 'id="settingsChatTodosInChat"' in idx
    assert 'settings_label_chat_todos_in_chat' in idx
    assert 'settings_desc_chat_todos_in_chat' in idx
    assert "const chatTodosCb=$('settingsChatTodosInChat')" in panels
    assert "_chatTodosToggleEnabled(this.checked)" in panels
    assert "typeof chatTodosEnabled==='function'" in panels


def test_i18n_keys_registered_in_english_locale():
    i18n = _read_static("static/i18n.js")
    assert "settings_label_chat_todos_in_chat: 'Show task list in chat'" in i18n
    assert "settings_desc_chat_todos_in_chat: 'Show a collapsible task list" in i18n


def test_scheduler_fans_out_to_chat_todos_renderer():
    ui = _read_static("static/ui.js")
    block_start = ui.find("function scheduleTodosRefresh()")
    block_end = ui.find("function _resetTodosRenderCache()", block_start)
    assert block_start != -1 and block_end != -1
    scheduler = ui[block_start:block_end]
    # Both the non-RAF fallback and the RAF path must call renderChatTodos.
    assert scheduler.count("renderChatTodos()") >= 2


def test_chat_todos_renderer_escapes_content_and_marks_terminal_states():
    ui = _read_static("static/ui.js")
    start = ui.find("function renderChatTodos()")
    end = ui.find("function toggleChatTodos()", start)
    assert start != -1 and end != -1
    render = ui[start:end]

    # Guard against running in non-DOM contexts (node VM tests).
    assert "typeof $!=='function'" in render
    # User content must be escaped; never interpolated raw.
    assert "esc(content)" in render
    # Terminal states get muted + strikethrough.
    assert "line-through" in render
    assert "status==='completed'||status==='cancelled'" in render
    # Summary/counter derive from statuses, not from message text.
    assert "status!=='completed'" in ui and "status!=='cancelled'" in ui


def test_rail_hide_helper_targets_todos_panel_and_bounces_to_chat():
    ui = _read_static("static/ui.js")
    start = ui.find("function _syncChatTodosRailVisibility()")
    end = ui.find("function _chatTodosToggleEnabled", start)
    assert start != -1 and end != -1
    helper = ui[start:end]

    assert 'querySelectorAll(\'[data-panel="todos"]\')' in helper
    assert "nav-tab-hidden" in helper
    assert "switchPanel('chat'" in helper


def test_chat_todos_pref_defaults_to_enabled():
    ui = _read_static("static/ui.js")
    start = ui.find("function _chatTodosReadPref()")
    end = ui.find("function _chatTodosWritePref", start)
    assert start != -1 and end != -1
    pref = ui[start:end]

    assert "if(v===null) return true;" in pref  # default: enabled
    assert "v==='1'" in pref


def test_chat_todos_pref_persists_explicit_disabled():
    # Review blocker: toggling OFF then reloading must stay OFF. The writer must
    # store an explicit '0' instead of removing the key (which would collide
    # with the null => enabled first-use default).
    ui = _read_static("static/ui.js")
    start = ui.find("function _chatTodosWritePref")
    end = ui.find("function chatTodosEnabled()", start)
    assert start != -1 and end != -1
    writer = ui[start:end]

    assert "localStorage.setItem(CHAT_TODOS_LS_KEY,v?'1':'0')" in writer
    assert "removeItem(CHAT_TODOS_LS_KEY)" not in writer


def test_chat_todos_aria_initial_collapsed():
    idx = _read_static("static/index.html")
    # Tray markup is hidden + collapsed by default; ARIA must match.
    assert 'id="chatTodosHead"' in idx
    head_start = idx.find('id="chatTodosHead"')
    # The head button's initial aria-expanded must be false (collapsed) and
    # must own the body region for a11y tree correctness.
    head_snippet = idx[head_start - 200 : head_start + 400]
    assert 'aria-expanded="false"' in head_snippet
    assert 'aria-controls="chatTodosBody"' in head_snippet
    # toggleChatTodos must flip aria-expanded to stay in sync.
    ui = _read_static("static/ui.js")
    toggle_start = ui.find("function toggleChatTodos()")
    assert toggle_start != -1
    toggle = ui[toggle_start : toggle_start + 600]
    assert "setAttribute('aria-expanded'" in toggle
    assert "isOpen?'true':'false'" in toggle or 'isOpen ?' in toggle


def test_chat_todos_hidden_tab_collision():
    # Desktop absolute tray: the sidebar Todos nav entry must stay hidden
    # whenever the in-chat tray is enabled, regardless of the per-profile
    # hidden_tabs setting. Otherwise a profile switch can restore it.
    panels = _read_static("static/panels.js")
    idx = _read_static("static/index.html")
    # panels.js re-applies the preference inside the applied-visibility pass.
    assert "if(panel==='todos'&&chatTodosOn) shouldHide=true;" in panels
    assert "chatTodosOn=(typeof chatTodosEnabled==='function'?chatTodosEnabled():false)" in panels
    # The synchronous boot IIFE in index.html must also hide the Todos tab
    # before first paint when the preference is default/enabled.
    assert "hermes-webui-chat-todos" in idx
    assert "p.indexOf('todos')===-1)p.push('todos')" in idx


def test_chat_todos_i18n_keys_in_all_locales():
    src = _read_static("static/i18n.js")
    # Extract en block keys that are chat-todos specific
    expected = {
        "settings_label_chat_todos_in_chat",
        "settings_desc_chat_todos_in_chat",
        "settings_label_chat_todos_align",
        "settings_option_chat_todos_align_left",
        "settings_option_chat_todos_align_center",
        "settings_option_chat_todos_align_right",
    }
    # LOCALES segmentation: each locale starts at "  <code>: {" and ends before
    # the next locale header. Using header boundaries avoids a fragile
    # balanced-brace scan that trips on `${...}` template literals inside i18n
    # (many _label helpers contain them). The same contract is verified by the
    # per-locale parity tests (test_chinese_locale.py etc.) which use a full
    # quote-aware extractor — here we assert presence of the 6 chat-todos keys.
    header_re = re.compile(r"^\s+'?([a-zA-Z-]+)'?\s*:\s*\{", re.MULTILINE)
    locale_headers = [
        (m.start(), m.group(1))
        for m in header_re.finditer(src)
        if "_lang" in src[m.end() : m.end() + 800]
    ]
    assert len(locale_headers) >= 14, f"expected >=14 locales, got {locale_headers}"
    for i, (start, locale_key) in enumerate(locale_headers):
        end = locale_headers[i + 1][0] if i + 1 < len(locale_headers) else len(src)
        block = src[start:end]
        missing = sorted(k for k in expected if k not in block)
        assert not missing, f"{locale_key} missing chat-todos keys: {missing}"


def test_chat_todos_desktop_does_not_push_message_stream():
    # Desktop tray is absolutely positioned above the messages so the transcript
    # never wastes the vertical band beside the tray. Mobile falls back to
    # static in-flow layout. This is a screenshot gate: the assertions bind
    # the visual contract the screenshot verifies.
    css = _read_static("static/style.css")
    # Desktop: absolute, out-of-flow; alignment variants via data-align.
    assert ".chat-todos{position:absolute;" in css
    assert ".chat-todos[data-align=\"center\"]{left:50%;" in css
    assert ".chat-todos[data-align=\"right\"]{left:auto;right:16px;" in css
    # Mobile: back to static full-width in-flow so phones read naturally.
    assert "@media(max-width:768px)" in css
    mobile_block_start = css.find("@media(max-width:768px)")
    mobile_block = css[mobile_block_start : mobile_block_start + 1200]
    assert ".chat-todos{position:static;" in mobile_block
