"""Regression coverage for #2462 stale /theme i18n help strings."""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def _locale_block(locale: str) -> str:
    # Locale keys are mostly bare identifiers, but zh-Hant is quoted. Match the
    # requested block up to the next top-level locale block or the LOCALES close.
    match = re.search(
        rf"\n\s*['\"]?{re.escape(locale)}['\"]?:\s*\{{(?P<body>.*?)(?=\n\s*['\"]?[a-z][\w-]*['\"]?:\s*\{{|\n\}};)",
        I18N_JS,
        re.S,
    )
    assert match, f"locale block {locale!r} not found"
    return match.group("body")


def _literal_value(block: str, key: str) -> str:
    match = re.search(rf"\n\s*{re.escape(key)}:\s*'(?P<value>(?:\\'|[^'])*)',", block)
    assert match, f"{key!r} not found in locale block"
    return match.group("value")


def test_theme_command_help_mentions_current_theme_and_skin_values():
    """Every /theme help string should describe the current Theme × Skin contract."""
    required_fragments = (
        "system/dark/light",
        "default/ares/mono/graphite/slate/poseidon/sisyphus/charizard/sienna/catppuccin/nous/geist-contrast",
    )
    for locale in ("en", "it", "ja", "ru", "es", "de", "zh", "zh-Hant", "pt", "ko", "fr", "tr"):
        value = _literal_value(_locale_block(locale), "cmd_theme")
        for fragment in required_fragments:
            assert fragment in value, f"{locale} cmd_theme missing {fragment!r}: {value!r}"


def test_french_theme_usage_uses_actual_slash_command_with_space():
    fr_theme_usage = _literal_value(_locale_block("fr"), "theme_usage")
    assert fr_theme_usage == "Utilisation : /theme "
    assert "/thème" not in fr_theme_usage


def test_spanish_mcp_labels_do_not_fall_back_to_chinese_placeholders():
    """The MCP settings surface must not mix Spanish controls with stale Chinese copy."""
    block = _locale_block("es")
    visible_literals = (
        "mcp_servers_title", "mcp_servers_desc", "mcp_no_servers", "mcp_add_server",
        "mcp_field_name", "mcp_transport_label", "mcp_field_command", "mcp_field_timeout",
        "mcp_save", "mcp_cancel", "mcp_name_required", "mcp_url_required",
        "mcp_command_required", "mcp_saved", "mcp_save_failed", "mcp_delete_confirm_title",
        "mcp_delete_confirm_message", "mcp_deleted", "mcp_delete_failed", "mcp_load_failed",
    )
    for key in visible_literals:
        value = _literal_value(block, key)
        assert not re.search(r"[\u3400-\u9fff]", value), f"Spanish {key} contains Chinese placeholder: {value!r}"
    assert _literal_value(block, "mcp_servers_title") == "Servidores MCP"
