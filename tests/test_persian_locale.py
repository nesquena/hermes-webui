"""Persian (fa) locale + locale-aware RTL direction.

#6664: add a Persian locale (`fa`, speech `fa-IR`) and make the application
shell follow locale direction metadata so Persian and future right-to-left
locales render correctly. The locale declares `_dir: 'rtl'`; setLocale()
applies it to <html dir> and restores `ltr` for non-RTL locales.
"""
from collections import Counter
from pathlib import Path
import re
from tests.test_issue2147_profile_concept_help import PROFILE_CONCEPT_KEYS


REPO = Path(__file__).resolve().parent.parent
PROFILE_CONCEPT_FALLBACK_KEYS = {
    *PROFILE_CONCEPT_KEYS,
    "workspace_artifact_source_session",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_locale_block(src: str, locale_key: str) -> str:
    start_match = re.search(rf"\b{re.escape(locale_key)}\s*:\s*\{{", src)
    assert start_match, f"{locale_key} locale block not found"

    start = start_match.end() - 1
    depth = 0
    in_single = False
    in_double = False
    in_backtick = False
    escape = False

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


def locale_keys(src: str, locale_key: str) -> list[str]:
    key_pattern = re.compile(r"^\s*([a-zA-Z0-9_]+)\s*:", re.MULTILINE)
    return key_pattern.findall(extract_locale_block(src, locale_key))


def test_persian_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    fa_block = extract_locale_block(src, "fa")
    assert fa_block
    assert "_lang: 'fa'" in fa_block
    assert "_label: 'فارسی'" in fa_block
    assert "_speech: 'fa-IR'" in fa_block
    assert "_dir: 'rtl'" in fa_block


def test_persian_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    fa_block = extract_locale_block(src, "fa")
    expected = [
        "settings_title: 'تنظیمات'",
        "settings_label_language: 'زبان'",
        "login_title: 'ورود'",
        "approval_heading: 'تأیید لازم است'",
        "tab_chat: 'گفتگو'",
        "tab_tasks: 'وظایف'",
        "empty_title: 'در چه کاری می‌توانم کمک کنم؟'",
        "onboarding_title: 'به رابط کاربری وب هرمس خوش آمدید'",
    ]
    for entry in expected:
        assert entry in fa_block


def test_persian_locale_matches_english_key_coverage():
    src = read(REPO / "static" / "i18n.js")
    en_keys = set(locale_keys(src, "en"))
    fa_keys = set(locale_keys(src, "fa"))
    assert sorted((en_keys - fa_keys) - PROFILE_CONCEPT_FALLBACK_KEYS) == []
    assert sorted(fa_keys - en_keys - {"_dir"}) == []


def test_persian_locale_has_no_new_duplicate_keys():
    """fa may only duplicate keys that en itself duplicates (shared by design)."""
    src = read(REPO / "static" / "i18n.js")
    en_dups = {k for k, c in Counter(locale_keys(src, "en")).items() if c > 1}
    fa_dups = {k for k, c in Counter(locale_keys(src, "fa")).items() if c > 1}
    assert not (fa_dups - en_dups), f"Persian locale has new duplicate keys: {fa_dups - en_dups}"


def test_persian_locale_keys_use_standard_indentation():
    src = read(REPO / "static" / "i18n.js")
    fa_block = extract_locale_block(src, "fa")
    badly_indented = [
        line.strip()
        for line in fa_block.splitlines()
        if re.match(r"^\s{1,3}[a-zA-Z0-9_]+ *:", line)
    ]
    assert badly_indented == []


def test_persian_locale_has_no_double_escaped_unicode_sequences():
    """JSON-style double escapes (\\\\u2026) render literal backslash-u in the UI."""
    src = read(REPO / "static" / "i18n.js")
    fa_block = extract_locale_block(src, "fa")
    for bad in ("\\\\u2026", "\\\\u2192", "\\\\u2713"):
        assert bad not in fa_block, f"Persian locale must not contain {bad!r}"


def test_set_locale_applies_rtl_direction_for_persian():
    """setLocale('fa') must set dir=rtl on <html>; non-RTL locales restore ltr."""
    src = read(REPO / "static" / "i18n.js")
    assert "_dir === 'rtl'" in src, (
        "setLocale() must apply the locale's _dir metadata to <html dir>"
    )
    assert "document.documentElement.dir" in src, (
        "setLocale() must write document.documentElement.dir"
    )
    # Non-RTL locales must explicitly restore LTR so an RTL selection never
    # leaks into the next session or a locale switch.
    assert "'ltr'" in src


def test_rtl_shell_css_keeps_technical_surfaces_ltr():
    """RTL shell CSS must bidi-isolate code/terminal/path/numeric surfaces."""
    css = read(REPO / "static" / "style.css")
    assert 'html[dir="rtl"]' in css
    assert "unicode-bidi:isolate" in css
    assert "unicode-bidi:plaintext" in css
    # Technical surfaces stay LTR even in an RTL shell.
    rtl_block_start = css.index('html[dir="rtl"]')
    rtl_block = css[rtl_block_start:]
    assert "direction:ltr" in rtl_block
    assert ".composer-terminal-surface" in rtl_block
    assert 'input[type="number"]' in rtl_block
    # The sidebar border flips so the right-side rail keeps its divider.
    assert "border-left:1px solid var(--border)" in rtl_block
