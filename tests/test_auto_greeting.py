"""Regression coverage for the opt-in automatic greeting in new chats."""
from pathlib import Path
import json

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "api" / "config.py"
INDEX = REPO_ROOT / "static" / "index.html"
BOOT = REPO_ROOT / "static" / "boot.js"
PANELS = REPO_ROOT / "static" / "panels.js"
SESSIONS = REPO_ROOT / "static" / "sessions.js"
I18N = REPO_ROOT / "static" / "i18n.js"


def test_setting_defaults_off_and_is_boolean(monkeypatch, tmp_path):
    import api.config as config

    assert config._SETTINGS_DEFAULTS["auto_greet_new_chat"] is False
    assert "auto_greet_new_chat" in config._SETTINGS_BOOL_KEYS

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    assert config.load_settings()["auto_greet_new_chat"] is False
    saved = config.save_settings({"auto_greet_new_chat": True})
    assert saved["auto_greet_new_chat"] is True
    assert json.loads(settings_path.read_text())["auto_greet_new_chat"] is True


def test_settings_control_is_present_and_wired():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="settingsAutoGreetNewChat"' in html
    assert 'data-i18n="settings_label_auto_greet_new_chat"' in html
    assert 'data-i18n="settings_desc_auto_greet_new_chat"' in html

    boot = BOOT.read_text(encoding="utf-8")
    assert "window._autoGreetNewChat=s.auto_greet_new_chat===true" in boot
    assert "if(window._autoGreetNewChat===true && !S.session" in boot

    panels = PANELS.read_text(encoding="utf-8")
    assert "settingsAutoGreetNewChat" in panels
    assert "payload.auto_greet_new_chat=autoGreetNewChatCb.checked;" in panels

    i18n = I18N.read_text(encoding="utf-8")
    assert "settings_label_auto_greet_new_chat:" in i18n
    assert "settings_desc_auto_greet_new_chat:" in i18n


def test_new_session_has_one_shot_greeting_but_load_session_does_not():
    source = SESSIONS.read_text(encoding="utf-8")
    new_start = source.index("async function newSession(")
    load_start = source.index("async function loadSession(")
    new_source = source[new_start:load_start]
    load_source = source[load_start:]

    assert "window._autoGreetNewChat===true" in new_source
    assert "Please greet me briefly" in new_source
    assert "_autoGreetedSessionIds" in new_source
    assert "await send(" in new_source
    assert "window._autoGreetNewChat===true" not in load_source
    assert "title:'New conversation'" in new_source


def test_greeting_prompt_is_sent_as_a_normal_user_turn():
    source = SESSIONS.read_text(encoding="utf-8")
    assert "autoGreeting" in source
    assert "send({autoGreeting:true})" in source or "send({ autoGreeting: true })" in source
    messages = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    assert "autoGreeting" in messages
    assert "literalSlash" in messages


def test_automatic_greeting_is_internal_to_the_visible_transcript():
    messages = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    ui = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    sessions = SESSIONS.read_text(encoding="utf-8")
    assert "_auto_greeting:true" in messages
    assert "!m._auto_greeting" in ui
    assert "autoGreeting?{_auto_greeting:true}" in messages
    assert "!autoGreeting&&S.session" in messages
    assert "_auto_greeting" not in sessions
