"""
Sprint 33 Tests: Shared app dialogs replace native confirm/prompt usage.

These tests verify the static assets expose the reusable confirm/input modal
and that browser-native confirm/prompt calls are no longer used in the Web UI.
"""

import pathlib
import re


REPO = pathlib.Path(__file__).parent.parent


def read(path):
    return (REPO / path).read_text(encoding="utf-8")


def test_index_has_shared_app_dialog_markup():
    html = read("static/index.html")
    assert 'id="appDialogOverlay"' in html
    assert 'id="appDialog"' in html
    assert 'id="appDialogTitle"' in html
    assert 'id="appDialogDesc"' in html
    assert 'id="appDialogInput"' in html
    assert 'id="appDialogCancel"' in html
    assert 'id="appDialogConfirm"' in html


def test_app_dialog_css_rules_exist():
    css = read("static/style.css")
    for selector in (
        ".app-dialog-overlay",
        ".app-dialog",
        ".app-dialog-input",
        ".app-dialog-actions",
        ".app-dialog-btn.confirm",
        ".app-dialog-btn.confirm.danger",
    ):
        assert selector in css, f"missing CSS selector: {selector}"


def test_ui_js_exposes_shared_dialog_helpers():
    src = read("static/ui.js")
    assert "function showConfirmDialog(opts={})" in src
    assert "function showPromptDialog(opts={})" in src
    assert "document.addEventListener('keydown'" in src


def test_prompt_dialog_honors_custom_label_and_danger_state():
    src = read("static/ui.js")
    assert "confirmBtn.textContent=opts.confirmLabel||t('create')" in src
    assert "confirmBtn.classList.toggle('danger',!!opts.danger)" in src
    assert "opts.danger?'alertdialog':'dialog'" in src


def test_disable_auth_prompt_uses_destructive_label_not_create():
    src = read("static/panels.js")
    match = re.search(r"async function disableAuth\(\)\{(.*?)\n\}", src, re.DOTALL)
    assert match, "disableAuth() not found"
    body = match.group(1)
    assert "const confirmText='DISABLE AUTH'" in body
    assert "currentPwField=$('settingsCurrentPassword')" in body
    assert "showToast(t('current_password_required'))" in body
    assert body.index("showToast(t('current_password_required'))") < body.index("showPromptDialog")
    assert "showPromptDialog" in body
    assert "confirmLabel:t('disable_auth')" in body
    assert "danger:true" in body
    assert "t('create')" not in body


def test_auth_disabled_warning_uses_status_payload_without_extra_settings_fetch():
    src = read("static/panels.js")
    match = re.search(r"function _updateAuthDisabledWarning\(authStatus\)\{(.*?)\n\}", src, re.DOTALL)
    assert match, "_updateAuthDisabledWarning(authStatus) not found"
    body = match.group(1)
    assert "authStatus&&authStatus.auth_disabled_acknowledged" in body
    assert "api('/api/settings')" not in body


def test_acknowledgement_save_failure_uses_i18n_toast():
    src = read("static/panels.js")
    match = re.search(r"async function _setAuthDisabledAck\(checked\)\{(.*?)\n\}", src, re.DOTALL)
    assert match, "_setAuthDisabledAck(checked) not found"
    body = match.group(1)
    assert "showToast(t('auth_ack_save_failed')+e.message)" in body
    assert "Failed to update acknowledgement" not in body


def test_save_settings_password_change_preflights_current_password_before_api():
    src = read("static/panels.js")
    match = re.search(r"async function saveSettings\([^)]*\)\{(.*?)\n\}", src, re.DOTALL)
    assert match, "saveSettings() not found"
    body = match.group(1)
    assert "currentPwField=$('settingsCurrentPassword')" in body
    assert "showToast(t('current_password_required'))" in body
    assert body.index("showToast(t('current_password_required'))") < body.index("api('/api/settings'")


def test_disable_auth_typed_confirm_locales_show_literal_phrase():
    src = read("static/i18n.js")
    values = re.findall(r"disable_auth_typed_confirm:\s*'([^']*)'", src)
    assert values, "disable_auth_typed_confirm keys missing"
    assert len(values) == len(_i18n_locale_blocks(src)), (
        "Every locale must define disable_auth_typed_confirm exactly once"
    )
    missing_literal = [value for value in values if "DISABLE AUTH" not in value]
    assert not missing_literal, (
        "Disable-auth prompt must display the exact phrase accepted by "
        f"disableAuth(), but these translations do not: {missing_literal}"
    )


AUTH_SAFETY_LOCALE_KEYS = (
    "current_password_label",
    "current_password_placeholder",
    "current_password_required",
    "current_password_incorrect",
    "disable_auth_typed_confirm",
    "auth_status_password",
    "auth_status_passkey_only",
    "auth_status_unauthenticated",
    "auth_warning_badge",
    "auth_disabled_warning_message",
    "auth_acknowledged_label",
    "auth_ack_save_failed",
)


def _i18n_locale_blocks(src):
    heads = list(re.finditer(r"^  (?:(?:'([^']+)')|([A-Za-z][A-Za-z0-9_]*)):\s*\{", src, re.M))
    blocks = {}
    for i, head in enumerate(heads):
        locale = head.group(1) or head.group(2)
        end = heads[i + 1].start() if i + 1 < len(heads) else src.find("\n};", head.end())
        assert end != -1, f"could not find end of locale block {locale}"
        blocks[locale] = src[head.end():end]
    return blocks


def test_auth_safety_keys_exist_once_per_locale():
    src = read("static/i18n.js")
    blocks = _i18n_locale_blocks(src)
    assert "pt" in blocks
    assert "zh-Hant" in blocks
    for locale, block in blocks.items():
        missing = [key for key in AUTH_SAFETY_LOCALE_KEYS if f"{key}:" not in block]
        duplicated = [
            key for key in AUTH_SAFETY_LOCALE_KEYS
            if len(re.findall(rf"\b{re.escape(key)}\s*:", block)) != 1
        ]
        assert not missing, f"{locale} missing auth-safety locale keys: {missing}"
        assert not duplicated, f"{locale} has duplicated auth-safety locale keys: {duplicated}"


def test_auth_safety_pt_and_zh_hant_strings_stay_in_correct_locale_blocks():
    src = read("static/i18n.js")
    blocks = _i18n_locale_blocks(src)
    zh_hant = blocks["zh-Hant"]
    pt = blocks["pt"]
    assert "目前密碼" in zh_hant
    assert "輸入 DISABLE AUTH" in zh_hant
    assert "Senha atual" not in zh_hant
    assert "Digite DISABLE AUTH" not in zh_hant
    assert "Senha atual" in pt
    assert "Digite DISABLE AUTH" in pt


def test_no_native_confirm_calls_remain_in_static_js():
    for path in (REPO / "static").glob("*.js"):
        src = path.read_text(encoding="utf-8")
        assert not re.search(r"\bconfirm\s*\(", src), f"native confirm() remains in {path.name}"


def test_no_native_prompt_calls_remain_in_static_js():
    for path in (REPO / "static").glob("*.js"):
        src = path.read_text(encoding="utf-8")
        assert not re.search(r"\bprompt\s*\(", src), f"native prompt() remains in {path.name}"
