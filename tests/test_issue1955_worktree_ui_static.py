from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_session_new_route_accepts_worktree_flag_and_uses_worktree_info():
    src = read("api/routes.py")
    assert "create_worktree_for_workspace" in src
    assert 'body.get("worktree")' in src or "body.get('worktree')" in src
    assert "worktree_info=" in src


def test_new_session_request_can_include_worktree_flag():
    src = read("static/sessions.js")
    assert "async function newSession(flash, options={})" in src
    assert "reqBody.worktree=true" in src


def test_workspace_dropdown_exposes_new_worktree_conversation_action():
    src = read("static/panels.js")
    assert "workspace_new_worktree_conversation" in src
    assert "workspace_new_worktree_conversation_meta" in src
    assert "newSession(false,{worktree:true})" in src
    assert "li('git-branch',12)" in src


def test_normal_new_conversation_defaults_to_worktree_setting():
    config = read("api/config.py")
    index = read("static/index.html")
    boot = read("static/boot.js")
    panels = read("static/panels.js")
    sessions = read("static/sessions.js")

    assert '"new_conversation_worktree_default": True' in config
    bool_keys = config[config.index("_SETTINGS_BOOL_KEYS") :]
    assert '"new_conversation_worktree_default"' in bool_keys

    assert 'id="settingsNewConversationWorktreeDefault"' in index
    assert 'data-i18n="settings_label_new_conversation_worktree_default"' in index
    assert 'data-i18n="settings_desc_new_conversation_worktree_default"' in index

    assert "window._newConversationWorktreeDefault=s.new_conversation_worktree_default!==false" in boot
    assert "window._newConversationWorktreeDefault=true" in boot

    assert "new_conversation_worktree_default: !!($('settingsNewConversationWorktreeDefault')||{}).checked" in panels
    assert "Object.prototype.hasOwnProperty.call(saved,'new_conversation_worktree_default')" in panels
    assert "!!(payload&&payload.new_conversation_worktree_default)" in panels
    assert "newWorktreeDefaultCb.checked=settings.new_conversation_worktree_default!==false" in panels
    assert "body.new_conversation_worktree_default=!!($('settingsNewConversationWorktreeDefault')||{}).checked" in panels

    assert "const worktreeRequested=Object.prototype.hasOwnProperty.call(options||{},'worktree')" in sessions
    assert "window._newConversationWorktreeDefault!==false" in sessions
    assert "if(worktreeRequested) reqBody.worktree=true;" in sessions
    assert "const defaultWorktreeRequested=worktreeRequested&&!Object.prototype.hasOwnProperty.call(options||{},'worktree')" in sessions
    assert "delete fallbackBody.worktree" in sessions
    assert "new_conversation_worktree_fallback" in sessions


def test_new_conversation_worktree_default_i18n_keys_exist_for_all_locales():
    i18n = read("static/i18n.js")
    assert "settings_label_new_conversation_worktree_default" in i18n
    assert "settings_desc_new_conversation_worktree_default" in i18n
    assert i18n.count("settings_label_new_conversation_worktree_default") == i18n.count("settings_label_project_quick_create")
    assert i18n.count("settings_desc_new_conversation_worktree_default") == i18n.count("settings_desc_project_quick_create")


def test_session_sidebar_renders_worktree_indicator():
    src = read("static/sessions.js")
    assert "session-worktree-indicator" in src
    assert "s.worktree_path" in src
    assert "s.worktree_branch" in src


def test_worktree_indicator_styles_and_i18n_exist():
    css = read("static/style.css")
    i18n = read("static/i18n.js")
    assert ".session-worktree-indicator" in css
    assert "workspace_new_worktree_conversation" in i18n
    assert "session_worktree_badge" in i18n
