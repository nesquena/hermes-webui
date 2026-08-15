"""Static frontend tests for the in-chat todos tray (feat/todos-in-chat).

These verify that the chat-embedded task tray wiring stays intact:
  - the tray DOM exists in index.html next to the message shell
  - the settings checkbox exists and is wired in loadSettingsPanel
  - the scheduler fans out to renderChatTodos on every todo_state refresh
  - the render path escapes user content and marks terminal states
  - the rail-hide helper targets [data-panel="todos"] with nav-tab-hidden
"""

from __future__ import annotations

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
