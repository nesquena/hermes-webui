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
    assert "new URL(`/share/${encodeURIComponent(S.session.share_token)}`,location.origin).href" in BOOT_JS
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
    assert "new URL(`/share/${encodeURIComponent(token)}`,location.origin).href;" in SESSIONS_JS
    assert "api('/api/share/create'" in SESSIONS_JS
    assert "api('/api/share/revoke'" in SESSIONS_JS
    assert "t('stop_sharing_session')" in SESSIONS_JS


def test_public_share_page_does_not_render_provider_details_blocks():
    assert "provider-error-details" not in SHARE_JS
    assert "provider_details_label||'Provider details'" not in SHARE_JS


def test_share_action_buttons_are_not_duplicated():
    # Regression: a rebase double-applied the Share/StopSharing/ExportHTML button
    # triplet, leaving duplicate IDs so getElementById wired only the first copy
    # (three visible-but-inert buttons). Each control must appear exactly once.
    for _id in ("btnShareSession", "btnStopSharingSession", "btnExportHTML"):
        assert INDEX_HTML.count(f'id="{_id}"') == 1, f"{_id} must appear exactly once"


def test_share_snapshot_redaction_is_always_on_regardless_of_setting():
    # The public-share boundary must redact credentials + local paths and drop
    # non-text/tool/system content EVEN IF the operator disabled api_redact_enabled.
    import os, tempfile
    os.environ.setdefault("HERMES_WEBUI_STATE_DIR", tempfile.mkdtemp())
    import api.config as config
    import api.shares as shares
    # Force the user-toggleable API redaction OFF (redact_session_data reads
    # api.config.load_settings() at call time).
    _orig = config.load_settings
    config.load_settings = lambda: {"api_redact_enabled": False}
    try:
        class _S:
            pass
        s = _S()
        s.title = "Deploy sk-ABCDEF1234567890TOKEN"
        s.workspace = "/very/private/workspace"
        s.messages = [
            {"role": "system", "content": "internal system prompt stays private"},
            {"role": "user", "content": "key sk-SECRET1234567890abcdef path /very/private/workspace/x.py"},
            {"role": "assistant", "content": {"tool": "terminal", "output": "raw structured tool payload"}},
            # dict-valued "text" inside a text block (possible via /api/session/import)
            {"role": "assistant", "content": [{"type": "text", "text": {"tool": "terminal", "output": "STRUCTURED_SECRET_IN_TEXTBLOCK"}}]},
            {"role": "tool", "content": "raw tool output"},
            {"role": "assistant", "content": "Here is the answer."},
        ]
        snap = shares.build_share_snapshot(s)
        import json
        blob = json.dumps(snap)
        # credentials masked (in both title and message text)
        assert "sk-SECRET1234567890" not in blob
        assert "sk-ABCDEF1234567890" not in blob
        # local paths scrubbed
        assert "/very/private/workspace" not in blob
        # structured/dict content never stringified into the public payload
        assert "raw structured tool payload" not in blob
        assert "STRUCTURED_SECRET_IN_TEXTBLOCK" not in blob
        # system + tool roles excluded
        roles = [m["role"] for m in snap["messages"]]
        assert "system" not in roles and "tool" not in roles
        # the real answer is preserved
        assert any("Here is the answer" in m["content"] for m in snap["messages"])

    finally:
        config.load_settings = _orig


def test_share_snapshot_rejects_dict_valued_title():
    # A dict-valued title (possible via /api/session/import) must not be
    # stringified into the public snapshot — fall back to "Untitled".
    import os, tempfile
    os.environ.setdefault("HERMES_WEBUI_STATE_DIR", tempfile.mkdtemp())
    import api.shares as shares

    class _S:
        pass
    s = _S()
    s.title = {"leak": "STRUCTURED_TITLE_SECRET"}
    s.workspace = ""
    s.messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    snap = shares.build_share_snapshot(s)
    import json
    assert "STRUCTURED_TITLE_SECRET" not in json.dumps(snap)
    assert snap["title"] == "Untitled"
