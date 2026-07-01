from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
SHARE_HTML = (ROOT / "static" / "share.html").read_text(encoding="utf-8")
SHARE_JS = (ROOT / "static" / "share.js").read_text(encoding="utf-8")


def test_conversation_panel_has_share_actions():
    assert 'id="btnShareSession"' in INDEX_HTML
    assert 'id="btnStopSharingSession"' in INDEX_HTML
    assert 'data-i18n="share_session"' in INDEX_HTML
    assert 'data-i18n="stop_sharing_session"' in INDEX_HTML


def test_boot_js_wires_share_create_and_revoke():
    assert "$('btnShareSession').onclick=async()=>{" in BOOT_JS
    assert "api('/api/share/create'" in BOOT_JS
    assert "$('btnStopSharingSession').onclick=async()=>{" in BOOT_JS
    assert "api('/api/share/revoke'" in BOOT_JS
    assert "share_session_created" in BOOT_JS
    assert "share_session_revoked" in BOOT_JS


def test_panels_sync_exposes_share_status_and_button_states():
    assert "share_session_status_active" in PANELS_JS
    assert "setDisabled('btnShareSession'" in PANELS_JS
    assert "setDisabled('btnStopSharingSession'" in PANELS_JS


def test_share_i18n_keys_exist_in_english_locale():
    for key in [
        "share_session",
        "share_session_tooltip",
        "share_session_status_active",
        "share_session_existing_confirm",
        "share_session_copy_existing",
        "share_session_refresh_snapshot",
        "share_session_link_copied",
        "share_session_created",
        "share_session_failed",
        "stop_sharing_session",
        "stop_sharing_session_tooltip",
        "stop_sharing_session_confirm",
        "share_session_revoked",
        "share_session_revoke_failed",
    ]:
        assert f"{key}:" in I18N_JS


def test_public_share_page_assets_exist():
    assert "Hermes Shared Conversation" in SHARE_HTML
    assert "/static/style.css" in SHARE_HTML
    assert "/static/share.js" in SHARE_HTML
    assert "function _shareLoad()" in SHARE_JS
    assert "/api/share/" in SHARE_JS


def test_session_action_menu_exposes_public_share_actions():
    assert "_appendSessionShareActions(menu, session);" in SESSIONS_JS
    assert "function _createOrRefreshSessionShare(session){" in SESSIONS_JS
    assert "api('/api/share/create'" in SESSIONS_JS
    assert "api('/api/share/revoke'" in SESSIONS_JS
    assert "t('stop_sharing_session')" in SESSIONS_JS


def test_public_share_page_renders_provider_details_blocks():
    assert "provider-error-details" in SHARE_JS
    assert "provider_details_label||'Provider details'" in SHARE_JS
