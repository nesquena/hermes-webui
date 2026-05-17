import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def _locale_blocks_with_body(i18n_text: str):
    locale_blocks = re.findall(
        r"\n\s*(?:'(?P<quoted>[a-z]{2}(?:-[A-Z][A-Za-z]+)?)'|(?P<plain>[a-z]{2}(?:-[A-Z]{2})?))\s*:\s*\{(.*?)\n\s*\}(?:,|\n\s*};)",
        i18n_text,
        flags=re.S,
    )
    return [(quoted or plain, body) for quoted, plain, body in locale_blocks]


def _string_value(body: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}\s*:\s*'((?:\\.|[^'])*)'", body)
    assert match, f"missing {key}"
    return match.group(1)


def test_all_locale_theme_help_lists_current_theme_and_skin_contract():
    required_terms = [
        "system/dark/light",
        "sienna",
        "catppuccin",
        "nous",
    ]

    locale_blocks = _locale_blocks_with_body(I18N)
    assert len(locale_blocks) >= 11

    failures = []
    for locale, body in locale_blocks:
        cmd_theme = _string_value(body, "cmd_theme")
        missing = [term for term in required_terms if term not in cmd_theme]
        if missing:
            failures.append(f"{locale}: missing {', '.join(missing)} in cmd_theme")

    assert not failures, "\n".join(failures)


def test_french_theme_usage_uses_actual_slash_command():
    locale_blocks = dict(_locale_blocks_with_body(I18N))
    theme_usage = _string_value(locale_blocks["fr"], "theme_usage")

    assert "/theme " in theme_usage
    assert "/thème" not in theme_usage
