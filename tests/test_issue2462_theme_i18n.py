"""Regression coverage for #2462 stale /theme i18n help strings."""

from pathlib import Path
import re

from tests.i18n_locale_loader import locale_block, locale_codes

ROOT = Path(__file__).resolve().parents[1]


def _locale_block(locale: str) -> str:
    return locale_block(locale)


def _literal_value(block: str, key: str) -> str:
    for quote in ("'", '"'):
        match = re.search(
            rf"\n\s*{re.escape(key)}:\s*{quote}(?P<value>(?:\\.|[^{quote}\\])*){quote},",
            block,
        )
        if match:
            return match.group("value").replace(r"\/", "/")
    raise AssertionError(f"{key!r} not found in locale block")


def test_theme_command_help_mentions_current_theme_and_skin_values():
    """Every /theme help string should describe the current Theme × Skin contract."""
    required_fragments = (
        "system/dark/light",
        "default/ares/mono/graphite/slate/poseidon/sisyphus/charizard/sienna/catppuccin/nous/geist-contrast",
    )
    for locale in locale_codes():
        value = _literal_value(_locale_block(locale), "cmd_theme")
        for fragment in required_fragments:
            assert fragment in value, f"{locale} cmd_theme missing {fragment!r}: {value!r}"


def test_french_theme_usage_uses_actual_slash_command_with_space():
    fr_theme_usage = _literal_value(_locale_block("fr"), "theme_usage")
    assert fr_theme_usage == "Utilisation : /theme "
    assert "/thème" not in fr_theme_usage
