"""Frontend coverage for the WeCom (企业微信) card in the Messaging settings section.

Mirrors tests/test_messaging_feishu_panel.py. Asserts:
  * panels.js wires the WeCom card (loadMessagingPanel -> _buildWecomCard) with
    the masked-secret sentinel that matches the backend, hits the real endpoints,
    and carries both modes' fields in its payload.
  * Every new ``wecom_*`` i18n key exists in EVERY locale block (the repo enforces
    per-locale parity against ``en``).
"""
from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def extract_locale_block(src: str, locale_key: str) -> str:
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


# ── panels.js wiring ─────────────────────────────────────────────────────────


def test_messaging_panel_builds_wecom_card():
    js = read("static/panels.js")
    assert "function _buildWecomCard(" in js
    assert "_buildWecomCard(wecomCfg" in js


def test_wecom_masked_sentinel_matches_backend():
    js = read("static/panels.js")
    py = read("api/platforms/wecom.py")
    assert "const WECOM_MASKED_SECRET='__WECOM_SECRET_SET__';" in js
    assert 'MASKED_SENTINEL = "__WECOM_SECRET_SET__"' in py


def test_wecom_card_uses_real_endpoints():
    js = read("static/panels.js")
    assert "api('/api/platforms/wecom')" in js
    assert "'/api/platforms/wecom/validate'" in js
    assert "'/api/platforms/wecom'" in js


def test_wecom_card_payload_carries_both_modes():
    js = read("static/panels.js")
    for field in (
        "mode:'wecom'",
        "mode:'wecom_callback'",
        "bot_id:",
        "secret:",
        "websocket_url:",
        "dm_policy:",
        "group_policy:",
        "home_channel:",
        "callback_corp_id:",
        "callback_corp_secret:",
        "callback_agent_id:",
        "callback_token:",
        "callback_encoding_aes_key:",
        "callback_host:",
        "callback_port:",
        "restart:",
    ):
        assert field in js, f"save payload missing {field}"


# ── i18n parity ──────────────────────────────────────────────────────────────


NEW_KEYS = [
    "wecom_card_name",
    "wecom_card_meta",
    "wecom_mode",
    "wecom_mode_websocket",
    "wecom_mode_callback",
    "wecom_bot_id",
    "wecom_secret",
    "wecom_secret_configured",
    "wecom_websocket_url",
    "wecom_dm_policy",
    "wecom_dm_policy_open",
    "wecom_dm_policy_allowlist",
    "wecom_dm_policy_disabled",
    "wecom_dm_policy_pairing",
    "wecom_allowed_users",
    "wecom_allowed_users_placeholder",
    "wecom_group_policy",
    "wecom_group_policy_open",
    "wecom_group_policy_allowlist",
    "wecom_group_policy_disabled",
    "wecom_home_channel",
    "wecom_home_channel_placeholder",
    "wecom_callback_corp_id",
    "wecom_callback_corp_secret",
    "wecom_callback_agent_id",
    "wecom_callback_token",
    "wecom_callback_encoding_aes_key",
    "wecom_callback_host",
    "wecom_callback_port",
    "wecom_restart_after_save",
    "wecom_btn_validate",
    "wecom_btn_save",
    "wecom_validating",
    "wecom_validate_need_id",
    "wecom_validate_ok",
    "wecom_validate_failed",
    "wecom_validate_failed_generic",
    "wecom_saving",
    "wecom_saved",
    "wecom_save_failed",
    "wecom_save_failed_generic",
    "wecom_restart_ok",
    "wecom_restart_failed",
]

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
    assert not missing, f"Locales missing new wecom keys: {missing}"


def test_zh_has_localized_wecom_strings():
    src = read("static/i18n.js")
    block = extract_locale_block(src, "zh")
    assert "wecom_card_name: '企业微信'" in block
    assert "wecom_btn_validate: '验证'" in block
    assert "wecom_btn_save: '保存'" in block
