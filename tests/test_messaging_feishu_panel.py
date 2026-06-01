"""Frontend coverage for Task 3: the Messaging platforms settings section + Feishu card.

Asserts:
  * The settings side-menu button and pane markup exist in static/index.html.
  * panels.js wires the section (switchSettingsSection -> loadMessagingPanel) and
    builds the Feishu card with the masked-secret sentinel that matches the backend.
  * Every new ``messaging_*`` / ``feishu_*`` i18n key exists in EVERY locale block
    (the repo enforces per-locale parity against ``en`` — adding to only en/zh would
    break the other locale coverage tests, so the keys land in all of them).
"""
from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def extract_locale_block(src: str, locale_key: str) -> str:
    # zh-Hant is keyed as a quoted string ('zh-Hant':); other locales are bare
    # identifiers (en:, ja:, …). Build the opener accordingly.
    if locale_key.startswith("'"):
        pattern = rf"{re.escape(locale_key)}\s*:\s*\{{"
    else:
        pattern = rf"\b{re.escape(locale_key)}\s*:\s*\{{"
    start_match = re.search(pattern, src)
    assert start_match, f"{locale_key} locale block not found"
    start = start_match.end() - 1
    depth = 0
    in_single = in_double = in_backtick = escape = False
    for i in range(start, len(src)):
        ch = src[i]
        if escape:
            escape = False
            continue
        if in_single:
            if ch == "\\":
                escape = True
            elif ch == "'":
                in_single = False
            continue
        if in_double:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            continue
        if in_backtick:
            if ch == "\\":
                escape = True
            elif ch == "`":
                in_backtick = False
            continue
        if ch == "'":
            in_single = True
            continue
        if ch == '"':
            in_double = True
            continue
        if ch == "`":
            in_backtick = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1 : i]
    raise AssertionError(f"{locale_key} locale block braces are not balanced")


# ── index.html markup ────────────────────────────────────────────────────────


def test_messaging_menu_button_present():
    html = read("static/index.html")
    assert 'data-settings-section="messaging"' in html
    assert "switchSettingsSection('messaging')" in html
    assert 'data-i18n="messaging_tab_title"' in html


def test_messaging_pane_present():
    html = read("static/index.html")
    assert 'id="settingsPaneMessaging"' in html
    assert 'id="messagingPlatformsList"' in html
    assert 'data-i18n="messaging_section_title"' in html


# ── panels.js wiring ─────────────────────────────────────────────────────────


def test_switch_section_loads_messaging_panel():
    js = read("static/panels.js")
    assert "if(section==='messaging') loadMessagingPanel();" in js
    # The section key must be in both the validity guard and the pane map.
    assert "messaging:'Messaging'" in js
    assert "async function loadMessagingPanel(" in js
    assert "function _buildFeishuCard(" in js


def test_feishu_masked_sentinel_matches_backend():
    js = read("static/panels.js")
    py = read("api/platforms/feishu.py")
    assert "const FEISHU_MASKED_SECRET='__FEISHU_SECRET_SET__';" in js
    assert 'MASKED_SENTINEL = "__FEISHU_SECRET_SET__"' in py


def test_feishu_card_uses_real_endpoints():
    js = read("static/panels.js")
    assert "api('/api/platforms/feishu')" in js
    assert "'/api/platforms/feishu/validate'" in js
    assert "'/api/platforms/feishu'" in js
    # The save payload must carry the editable fields + restart flag.
    for field in (
        "connection_mode:",
        "webhook_host:",
        "verification_token:",
        "encrypt_key:",
        "allow_all_users:",
        "group_policy:",
        "require_mention:",
        "home_channel:",
        "restart:",
    ):
        assert field in js, f"save payload missing {field}"


# ── i18n parity ──────────────────────────────────────────────────────────────


NEW_KEYS = [
    "messaging_tab_title",
    "messaging_section_title",
    "messaging_section_meta",
    "messaging_loading",
    "messaging_load_failed",
    "messaging_status_configured",
    "messaging_status_not_configured",
    "feishu_card_name",
    "feishu_card_meta",
    "feishu_app_id",
    "feishu_app_secret",
    "feishu_secret_configured",
    "feishu_domain",
    "feishu_domain_feishu",
    "feishu_domain_lark",
    "feishu_connection_mode",
    "feishu_mode_websocket",
    "feishu_mode_webhook",
    "feishu_webhook_host",
    "feishu_webhook_port",
    "feishu_webhook_path",
    "feishu_verification_token",
    "feishu_encrypt_key",
    "feishu_allow_all_users",
    "feishu_allowed_users",
    "feishu_allowed_users_placeholder",
    "feishu_group_policy",
    "feishu_group_policy_open",
    "feishu_group_policy_disabled",
    "feishu_require_mention",
    "feishu_home_channel",
    "feishu_home_channel_placeholder",
    "feishu_restart_after_save",
    "feishu_btn_validate",
    "feishu_btn_save",
    "feishu_btn_restart",
    "feishu_validating",
    "feishu_validate_need_app_id",
    "feishu_validate_ok",
    "feishu_validate_ok_generic",
    "feishu_validate_failed",
    "feishu_validate_failed_generic",
    "feishu_saving",
    "feishu_saved",
    "feishu_save_failed",
    "feishu_save_failed_generic",
    "feishu_restart_ok",
    "feishu_restart_failed",
]

# zh-Hant is keyed as a quoted string in i18n.js ('zh-Hant':), so its locale
# marker differs from the bare-identifier locales.
ALL_LOCALES = ["en", "it", "ja", "ru", "es", "de", "zh", "'zh-Hant'", "pt", "ko", "fr", "tr"]


def test_new_keys_present_in_every_locale():
    src = read("static/i18n.js")
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    missing = {}
    for loc in ALL_LOCALES:
        keys = set(key_pattern.findall(extract_locale_block(src, loc)))
        gap = [k for k in NEW_KEYS if k not in keys]
        if gap:
            missing[loc] = gap
    assert not missing, f"Locales missing new messaging/feishu keys: {missing}"


def test_zh_has_localized_feishu_strings():
    src = read("static/i18n.js")
    block = extract_locale_block(src, "zh")
    # zh must carry genuine Simplified-Chinese values, not the English fallback.
    assert "messaging_section_title: '消息平台'" in block
    assert "feishu_btn_validate: '验证'" in block
    assert "feishu_btn_save: '保存'" in block
