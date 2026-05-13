from pathlib import Path
import re

I18N_JS = (Path(__file__).resolve().parents[1] / "static" / "i18n.js").read_text(encoding="utf-8")


def _extract_locale_block(locale: str, src: str) -> str:
    locale_key_re = re.compile(
        rf"(?m)^[ \t]{{2}}(?:'{re.escape(locale)}'|\"{re.escape(locale)}\"|{re.escape(locale)})\s*:\s*\{{"
    )
    start_match = locale_key_re.search(src)
    assert start_match is not None, f"Locale {locale!r} not found in i18n.js"

    brace_start = start_match.end() - 1
    assert brace_start != -1, f"Locale {locale!r} block has no opening brace"

    next_locale_re = re.compile(
        r"(?m)^[ \t]{2}(?:[A-Za-z]{2,3}(?:[-_][A-Za-z0-9_]+)?|'[A-Za-z]{2,3}(?:[-_][A-Za-z0-9_]+)?'|\"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9_]+)?\")\s*:\s*\{"
    )
    next_match = next_locale_re.search(src, pos=brace_start + 1)
    end = next_match.start() if next_match else len(src)

    depth = 0
    for idx in range(brace_start, end):
        char = src[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return src[brace_start : idx + 1]
    assert False, f"Locale {locale!r} block did not close cleanly"


def test_german_profile_skill_count_is_function():
    de_block = _extract_locale_block("de", I18N_JS)
    # German locale should pass count as an interpolation arg, not expose {count} verbatim.
    assert "profile_skill_count:" in de_block
    assert "{count} Fähigkeiten" not in de_block
    assert re.search(r"profile_skill_count:\s*\([^)]*\)\s*=>", de_block), (
        "profile_skill_count in de locale should be an arrow function, not a string template"
    )
