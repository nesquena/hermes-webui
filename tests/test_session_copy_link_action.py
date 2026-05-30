from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def test_session_action_menu_has_copy_link_action():
    assert "ICONS.link" in SESSIONS_JS
    assert "_appendSessionCopyLinkAction(menu, session);" in SESSIONS_JS
    assert "t('session_copy_link')" in SESSIONS_JS
    assert "t('session_copy_link_desc')" in SESSIONS_JS


def test_session_link_copies_internal_markdown_reference_not_external_url():
    assert "function _sessionInternalReferenceForSession" in SESSIONS_JS
    assert "session://${_sessionMarkdownUrlSid(sid)}" in SESSIONS_JS
    assert "_copyTextToClipboard(ref)" in SESSIONS_JS
    assert "function _sessionAbsoluteUrlForSid" not in SESSIONS_JS


def test_session_link_markdown_url_encodes_parentheses():
    assert "function _sessionMarkdownUrlSid" in SESSIONS_JS
    assert "encodeURIComponent(String(sid||''))" in SESSIONS_JS
    assert "replace(/[()]/g" in SESSIONS_JS
    assert "%28" in SESSIONS_JS
    assert "%29" in SESSIONS_JS


def test_session_link_label_collapses_multiline_titles():
    assert ".replace(/\\s+/g,' ')" in SESSIONS_JS


def test_copy_link_has_clipboard_fallback():
    assert "navigator.clipboard.writeText" in SESSIONS_JS
    assert "document.execCommand('copy')" in SESSIONS_JS
    assert "showToast(t('session_link_copied'))" in SESSIONS_JS
    assert "t('session_link_copy_failed')" in SESSIONS_JS


def test_read_only_sessions_can_still_open_actions_for_copy_link():
    start = SESSIONS_JS.index("function _openSessionActionMenu(session, anchorEl){")
    end = SESSIONS_JS.index("document.addEventListener('click'", start)
    open_menu_block = SESSIONS_JS[start:end]
    assert "Read-only imported sessions cannot be modified" not in open_menu_block
    assert "const isReadOnly = _isReadOnlySession(session);" in open_menu_block
    assert "if(isReadOnly){\n    _mountSessionActionMenu(menu, session, anchorEl);\n    return;\n  }" in open_menu_block


def test_copy_link_i18n_keys_have_english_and_german_labels():
    for key in [
        "session_copy_link",
        "session_copy_link_desc",
        "session_link_copied",
        "session_link_copy_failed",
    ]:
        assert I18N_JS.count(key) >= 2, f"{key} should be defined in English and German"
    assert "Copy conversation link" in I18N_JS
    assert "Unterhaltungslink kopieren" in I18N_JS


def test_rendered_session_reference_is_internal_link():
    assert "session:\\/\\/" in UI_JS
    assert "function _markdownAnchor" in UI_JS
    assert "class=\"session-link\"" in UI_JS
    assert "const sessionLink=e.target.closest('a.session-link[href]');" in UI_JS
    assert "loadSession(decodeURIComponent(m[1]))" in UI_JS


def test_streaming_markdown_keeps_session_refs_internal():
    assert "session:\\/\\/" in MESSAGES_JS
    assert "session-link" in MESSAGES_JS
    assert "_smdLinkHref" in MESSAGES_JS
    assert "^(file|workspace|session)" in MESSAGES_JS
